from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import hashlib
import json
import numpy as np
import pandas as pd
from PIL import Image
from pathlib import Path
import requests
import rasterio
import time
from typing import Any, Callable, Literal

from src.config import Settings
from src.domain.cache import load_cached_response, request_result_dir, save_cached_response
from src.domain.artifact_manifest import register_artifact
from src.domain.run_workspace import cleanup_run_tmp_dir, get_run_tmp_dir
from src.domain.stage_timing import StageTimingRecorder
from src.domain.bandon_runner import run_bandon_inference
from src.domain.change_products import derive_new_building_products
from src.domain.exports import create_export_bundle_from_manifest, export_bandon_outputs, write_geojson, write_run_manifest
from src.domain.imagery_providers import MapboxCurrentProvider
from src.domain.inference_reference_imagery import get_or_create_inference_reference_imagery
from src.domain.mapbox_current import MAPBOX_ATTRIBUTION, MAPBOX_SOURCE_ID
from src.domain.mosaic import MosaicResult, WaybackTileDownloadError, align_mosaic_pair, download_wayback_mosaic
from src.domain.tiled_inference import (
    TiledInferenceConfig,
    make_bandon_patch_predictor,
    run_tiled_inference,
    select_inference_mode,
)
from rasterio.features import rasterize
from shapely.geometry import shape

from src.domain.postprocess import (
    AdditionCandidateFilterSettings,
    filter_addition_candidates,
    remove_small_components,
    suppress_edge_hugging_components,
)
from src.domain.tiling import estimate_patch_count, intersecting_tiles_for_aoi
from src.domain.wayback_metadata_cache import (
    acquire_wayback_metadata_cache_lock,
    build_wayback_metadata_cache_key,
    build_wayback_metadata_cache_payload,
    get_wayback_metadata_cache_path,
    read_wayback_metadata_cache,
    write_wayback_metadata_cache_atomic,
)
from src.domain.wayback_tile_preflight_cache import (
    acquire_wayback_tile_preflight_cache_lock,
    build_wayback_tile_preflight_cache_key,
    build_wayback_tile_preflight_cache_payload,
    get_wayback_tile_preflight_cache_path,
    read_wayback_tile_preflight_cache,
    write_wayback_tile_preflight_cache_atomic,
)
from src.domain.vectorize import (
    VectorizationContext,
    build_building_blocks,
    build_change_blocks,
    build_change_buffer_layers,
    merge_close_change_regions,
    vectorize_change_regions,
)
from src.domain.wayback import (
    MetadataSummary,
    TileAvailabilitySummary,
    build_session,
    preflight_wayback_tile_availability,
    summarize_wayback_metadata,
)
from src.schemas import ArtifactEntry, DiagnosticMetadata, PreviewImages, RunRequest, RunResponse, SummaryStats, TabularMetrics
from src.services.releases import list_releases
from src.services.validation import (
    PreparedRequest,
    resolve_min_new_building_pixels,
    validate_request,
)
from src.utils.raster import rasterize_aoi_mask_like, read_rgb
from src.utils.geometry import reproject_geometry
from src.utils.logging import get_logger
from src.utils.profiling import StageTimings


LOGGER = get_logger(__name__)


DETECTION_STAGE_MAP = {
    "release_resolution": "backend_resolution",
    "tile_indexing": "imagery_cache_lookup",
    "download": "imagery_download_or_load",
    "mosaic": "coregistration",
    "bandon_inference": "inference",
    "postprocessing": "postprocess",
    "vectorization": "vectorization",
    "export": "artifact_write",
}


ProgressReporter = Callable[[float, str, dict[str, object] | None], None] | Callable[[float, str], None] | None


@dataclass(frozen=True)
class ResolvedWaybackRelease:
    release: Any
    zoom: int
    metadata: MetadataSummary
    tilemap: TileAvailabilitySummary | None


def _report(progress: ProgressReporter, value: float, message: str, details: dict[str, object] | None = None) -> None:
    if progress is not None:
        try:
            progress(value, message, details)  # type: ignore[misc]
        except TypeError:
            progress(value, message)  # type: ignore[misc]


def _wayback_download_progress_message(payload: dict[str, object]) -> str:
    rate = float(payload.get("tile_rate_per_sec") or 0.0)
    eta = payload.get("eta_seconds")
    eta_text = f", ETA {int(float(eta))}s" if isinstance(eta, (int, float)) and float(eta) > 0 else ""
    return (
        f"{payload.get('stage', 'Téléchargement des tuiles Wayback')} "
        f"z{payload.get('effective_zoom', '?')} "
        f"{payload.get('cache_hit_count', 0)} cache, "
        f"{payload.get('processed_tile_count', 0)}/{payload.get('total_tile_count', payload.get('selected_tile_count', 0))} traitées, "
        f"{payload.get('downloaded_tile_count', 0)} téléchargées, "
        f"{payload.get('failed_tile_count', 0)} échecs, "
        f"{payload.get('retry_count', 0)} retries, "
        f"{rate:.1f} tuiles/s{eta_text}, "
        f"missing_tile_ratio={float(payload.get('missing_tile_ratio') or 0.0):.3f}"
    )


def _probability_stats(values: np.ndarray, mask: np.ndarray | None = None) -> dict[str, float | None]:
    sample = values[mask] if mask is not None else values.reshape(-1)
    sample = sample.astype(np.float32, copy=False)
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return {
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "p01": None,
            "p05": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "fraction_ge_0_50": None,
            "fraction_ge_0_60": None,
            "fraction_ge_0_75": None,
            "fraction_ge_0_90": None,
        }
    return {
        "min": float(np.min(sample)),
        "max": float(np.max(sample)),
        "mean": float(np.mean(sample)),
        "std": float(np.std(sample)),
        "p01": float(np.percentile(sample, 1)),
        "p05": float(np.percentile(sample, 5)),
        "p50": float(np.percentile(sample, 50)),
        "p95": float(np.percentile(sample, 95)),
        "p99": float(np.percentile(sample, 99)),
        "fraction_ge_0_50": float(np.mean(sample >= 0.50)),
        "fraction_ge_0_60": float(np.mean(sample >= 0.60)),
        "fraction_ge_0_75": float(np.mean(sample >= 0.75)),
        "fraction_ge_0_90": float(np.mean(sample >= 0.90)),
    }


def _resolve_available_tiles_for_aoi(
    normalized_aoi: dict[str, Any],
    *,
    bbox: dict[str, float],
    zoom: int,
    preflight_available_tiles: frozenset[tuple[int, int]] | None,
) -> frozenset[tuple[int, int]] | None:
    intersecting_tiles, bbox_tile_count = intersecting_tiles_for_aoi(normalized_aoi, bbox=bbox, zoom=zoom)
    if intersecting_tiles is None:
        return preflight_available_tiles
    selected_tiles = (
        intersecting_tiles
        if preflight_available_tiles is None
        else frozenset(tile for tile in intersecting_tiles if tile in preflight_available_tiles)
    )
    LOGGER.info(
        "AOI_TILE_SELECTION bboxTileCount=%s intersectingTileCount=%s selectedTileCount=%s zoom=%s",
        bbox_tile_count,
        len(intersecting_tiles),
        len(selected_tiles),
        zoom,
    )
    return selected_tiles


def _inference_stage_message() -> str:
    return "Running local MTGCDNet change detection"


def _write_bandon_input_images(
    *,
    output_dir: Path,
    t1_image: Image.Image,
    t2_image: Image.Image,
) -> tuple[Path, Path]:
    t1_path = output_dir / "bandon_input_t1.png"
    t2_path = output_dir / "bandon_input_t2.png"
    t1_image.save(t1_path)
    t2_image.save(t2_path)
    return t1_path, t2_path


def _write_bandon_mask_png(output_dir: Path, name: str, mask: np.ndarray) -> Path:
    path = output_dir / name
    Image.fromarray((np.asarray(mask, dtype=bool).astype(np.uint8) * 255), mode="L").save(path)
    return path


def _feature_collection_from_geojsonl(path: Path, *, max_features: int = 25_000) -> tuple[dict[str, Any], int, bool]:
    features: list[dict[str, Any]] = []
    total = 0
    capped = False
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}, 0, False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            total += 1
            if len(features) < max_features:
                features.append(json.loads(line))
            else:
                capped = True
    return {"type": "FeatureCollection", "features": features}, total, capped


def _write_tiled_summary_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _rasterize_wgs84_feature_collection_like(
    feature_collection: dict[str, Any],
    reference_raster_path: Path,
) -> np.ndarray:
    with rasterio.open(reference_raster_path) as src:
        out_shape = (src.height, src.width)
        transform = src.transform
        crs = src.crs
    if crs is None:
        return np.zeros(out_shape, dtype=bool)

    geometries = []
    for feature in feature_collection.get("features", []):
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            continue
        try:
            geometry = shape(feature["geometry"]).buffer(0)
            if geometry.is_empty:
                continue
            native_geometry = reproject_geometry(geometry, "EPSG:4326", str(crs)).buffer(0)
        except Exception:
            continue
        if not native_geometry.is_empty:
            geometries.append(native_geometry)

    if not geometries:
        return np.zeros(out_shape, dtype=bool)
    return rasterize(
        [(geometry, 1) for geometry in geometries],
        out_shape=out_shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    ).astype(bool)


def _apply_aoi_mask_to_aligned_inputs(
    *,
    arr_t1: np.ndarray,
    arr_t2: np.ndarray,
    t1_valid_mask: np.ndarray,
    t2_valid_mask: np.ndarray,
    reference_raster_path: Path,
    normalized_aoi: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    aoi_mask = rasterize_aoi_mask_like(reference_raster_path, normalized_aoi)
    if aoi_mask.shape != t1_valid_mask.shape or aoi_mask.shape != t2_valid_mask.shape:
        raise ValueError(
            f"AOI mask shape {aoi_mask.shape} does not match aligned mask shapes "
            f"{t1_valid_mask.shape} / {t2_valid_mask.shape}."
        )
    if int(aoi_mask.sum()) == 0:
        LOGGER.warning(
            "AOI mask resolved to zero valid pixels for %s; skipping AOI mask application to preserve run stability.",
            reference_raster_path,
        )
        return arr_t1, arr_t2, np.asarray(t1_valid_mask, dtype=bool), np.asarray(t2_valid_mask, dtype=bool), aoi_mask
    t1_masked = np.asarray(t1_valid_mask, dtype=bool) & aoi_mask
    t2_masked = np.asarray(t2_valid_mask, dtype=bool) & aoi_mask
    arr_t1_masked = np.asarray(arr_t1).copy()
    arr_t2_masked = np.asarray(arr_t2).copy()
    arr_t1_masked[~t1_masked] = 0
    arr_t2_masked[~t2_masked] = 0
    LOGGER.info(
        "AOI_RASTER_MASK_APPLIED rasterPath=%s width=%s height=%s outsideAoiPixelCount=%s validInsideAoiPixelCount=%s",
        reference_raster_path,
        int(aoi_mask.shape[1]),
        int(aoi_mask.shape[0]),
        int((~aoi_mask).sum()),
        int(aoi_mask.sum()),
    )
    return arr_t1_masked, arr_t2_masked, t1_masked, t2_masked, aoi_mask


def _source_manifest_entries_for_scenes(
    *,
    request_dir: Path,
    run_id: str,
    scenes: list[MosaicResult],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for scene in scenes:
        source_label = "Mapbox" if scene.provider == "mapbox" else "Wayback"
        metadata = {
            "provider": scene.provider,
            "source_type": scene.source_type,
            "source_id": scene.source_id or scene.identifier,
            "effective_date": scene.effective_date or scene.release_date,
            "dominant_src_date": scene.dominant_src_date,
            "capture_date_known": scene.capture_date_known,
            "attribution_required": scene.provider == "mapbox",
            "attribution": scene.attribution,
        }
        entries.append(
            register_artifact(
                path=scene.geotiff_path,
                resolved_path=scene.geotiff_path,
                artifact_type="source",
                purpose=f"{source_label} source raster for {scene.identifier}",
                format="tif",
                keep_policy="cache",
                include_in_export=False,
                storage="request" if scene.materialized_in_request_dir else "shared_cache",
                request_dir=request_dir,
                run_id=run_id,
                cache_key=scene.cache_key,
                metadata=metadata,
            )
        )
        entries.append(
            register_artifact(
                path=scene.valid_mask_path,
                resolved_path=scene.valid_mask_path,
                artifact_type="source",
                purpose=f"{source_label} valid mask for {scene.identifier}",
                format="tif",
                keep_policy="cache",
                include_in_export=False,
                storage="request" if scene.materialized_in_request_dir else "shared_cache",
                request_dir=request_dir,
                run_id=run_id,
                cache_key=scene.cache_key,
                metadata=metadata,
            )
        )
    return entries


def _close_session_if_possible(session: object) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        close()


def _elapsed_ms(start_ns: int) -> float:
    return round((time.perf_counter_ns() - start_ns) / 1_000_000, 2)


def _release_resolution_stage_metadata(
    *,
    scene_role: str,
    release_id: str,
    zoom: int,
    min_zoom: int,
    attempt_index: int | None = None,
    tilemap_preflight_enabled: bool,
    metadata_status: str | None = None,
    preflight_status: str | None = None,
    coverage_ok: bool | None = None,
    fallback_used: bool | None = None,
    zoom_fallback_applied: bool | None = None,
    selected_zoom: int | None = None,
    selected_release: str | None = None,
    exception_class: str | None = None,
    cache_enabled: bool | None = None,
    cache_hit: bool | None = None,
    cache_key: str | None = None,
    cache_path_exists: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scene_role": scene_role,
        "release_id": release_id,
        "zoom": zoom,
        "min_zoom": min_zoom,
        "attempt_index": attempt_index,
        "tilemap_preflight_enabled": tilemap_preflight_enabled,
        "metadata_status": metadata_status,
        "preflight_status": preflight_status,
        "coverage_ok": coverage_ok,
        "fallback_used": fallback_used,
        "zoom_fallback_applied": zoom_fallback_applied,
        "selected_zoom": selected_zoom,
        "selected_release": selected_release,
        "exception_class": exception_class,
        "cache_enabled": cache_enabled,
        "cache_hit": cache_hit,
        "cache_key": cache_key,
        "cache_path_exists": cache_path_exists,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _safe_add_timing_stage(
    timing: StageTimingRecorder | None,
    name: str,
    *,
    duration_ms: float,
    status: str = "success",
    metadata: dict[str, Any] | None = None,
    error_type: str | None = None,
) -> None:
    if timing is None:
        return
    try:
        timing.add_stage(
            name,
            duration_ms=duration_ms,
            status=status,  # type: ignore[arg-type]
            metadata=metadata,
            error_type=error_type,
        )
    except Exception as exc:  # Timing diagnostics must never affect pipeline behavior.
        run_id = getattr(timing, "run_id", "unknown")
        LOGGER.warning("Failed to record release-resolution timing stage %s for %s: %s", name, run_id, exc)


def _write_timing_report_safely(timing: StageTimingRecorder, result_dir: Path) -> None:
    try:
        timing.write_timing_report(result_dir / "timing.json")
    except Exception as exc:  # Timing must never fail a successful pipeline run.
        LOGGER.warning("Failed to write timing report for %s: %s", timing.run_id, exc)


def _write_manifest_with_timing(
    *,
    timing: StageTimingRecorder,
    result_dir: Path,
    artifacts: list[Any],
    extra_artifacts: list[dict[str, object]] | None = None,
    run_metadata: dict[str, object] | None = None,
) -> None:
    with timing.stage("manifest_write"):
        write_run_manifest(result_dir, artifacts, extra_artifacts=extra_artifacts, run_metadata=run_metadata)
    _write_timing_report_safely(timing, result_dir)
    try:
        write_run_manifest(result_dir, artifacts, extra_artifacts=extra_artifacts, run_metadata=run_metadata)
    except Exception as exc:
        LOGGER.warning("Failed to refresh manifest with timing report for %s: %s", timing.run_id, exc)


def _sha256_file_or_none(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_path(path: Path, *, base: Path | None = None) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return ((base or Path.cwd()) / expanded).resolve()


def _detection_run_identity(
    *,
    settings: Settings,
    model_backend: str,
    request_hash: str,
    change_threshold: float,
    request_hash_context: dict[str, object] | None,
) -> dict[str, object]:
    checkpoint_path: Path | None = None
    config_path: Path | None = None
    if model_backend == "bandon_mps":
        checkpoint_path = _resolved_path(settings.bandon_checkpoint_path, base=settings.project_root)
        config_path = _resolved_path(settings.bandon_config_path, base=settings.bandon_repo_dir)
    checkpoint_sha256 = _sha256_file_or_none(checkpoint_path)
    identity: dict[str, object] = {
        "effective_backend": settings.inference_backend,
        "runner_family": "bandon_mps" if model_backend == "bandon_mps" else model_backend,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
        "checkpoint_sha256": checkpoint_sha256,
        "threshold": change_threshold,
        "config_path": str(config_path) if config_path else None,
        "request_hash": request_hash,
        "pair_hash": request_hash,
        "inference_cache_key": request_hash,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if request_hash_context is not None:
        identity["request_hash_context"] = request_hash_context
    return identity


def _log_effective_detection_identity(identity: dict[str, object]) -> None:
    LOGGER.info("EFFECTIVE_INFERENCE_BACKEND value=%s", identity.get("effective_backend"))
    LOGGER.info("EFFECTIVE_CHECKPOINT_PATH value=%s", identity.get("checkpoint_path"))
    LOGGER.info("EFFECTIVE_CHECKPOINT_SHA256 value=%s", identity.get("checkpoint_sha256"))
    LOGGER.info("EFFECTIVE_THRESHOLD value=%s", identity.get("threshold"))
    LOGGER.info("EFFECTIVE_RUN_CACHE_KEY value=%s", identity.get("inference_cache_key"))
    LOGGER.info("EFFECTIVE_REQUEST_HASH value=%s", identity.get("request_hash"))


def _summarize_release_metadata_for_request(
    settings: Settings,
    *,
    release_identifier: str,
    release_date: str | None,
    aoi_bbox: dict[str, float],
    normalized_aoi: dict[str, Any],
    zoom: int,
    timing: StageTimingRecorder | None = None,
    stage_prefix: str = "release_resolution",
    scene_role: str = "single",
    attempt_index: int | None = None,
    min_zoom: int | None = None,
    tilemap_preflight_enabled: bool = False,
) -> MetadataSummary:
    cache_key = build_wayback_metadata_cache_key(
        settings,
        release_identifier=release_identifier,
        release_date=release_date,
        bbox=aoi_bbox,
        aoi_geojson=normalized_aoi,
        zoom=zoom,
    )
    cache_path = get_wayback_metadata_cache_path(settings, cache_key)
    cache_enabled = settings.wayback_metadata_cache_enabled
    cache_path_exists = cache_path.exists()
    cache_hit = False
    summary: MetadataSummary | None = None

    def _live_lookup() -> MetadataSummary:
        session_setup_start = time.perf_counter_ns()
        session = build_session(settings)
        _safe_add_timing_stage(
            timing,
            f"{stage_prefix}.session_setup",
            duration_ms=_elapsed_ms(session_setup_start),
            metadata=_release_resolution_stage_metadata(
                scene_role=scene_role,
                release_id=release_identifier,
                zoom=zoom,
                min_zoom=min_zoom if min_zoom is not None else zoom,
                attempt_index=attempt_index,
                tilemap_preflight_enabled=tilemap_preflight_enabled,
                cache_enabled=cache_enabled,
                cache_hit=cache_hit,
                cache_key=cache_key,
                cache_path_exists=cache_path_exists,
            ),
        )
        try:
            return summarize_wayback_metadata(
                session,
                release_identifier,
                aoi_bbox,
                grid_size=settings.metadata_grid_size,
                aoi_geojson=normalized_aoi,
                zoom=zoom,
            )
        finally:
            _close_session_if_possible(session)

    lookup_start = time.perf_counter_ns()
    try:
        if cache_enabled:
            try:
                with acquire_wayback_metadata_cache_lock(cache_path):
                    cache_path_exists = cache_path.exists()
                    summary = read_wayback_metadata_cache(
                        cache_path,
                        cache_key=cache_key,
                        ttl_seconds=settings.wayback_metadata_cache_ttl_seconds,
                    )
                    cache_hit = summary is not None
                    if summary is None:
                        summary = _live_lookup()
                        try:
                            payload = build_wayback_metadata_cache_payload(
                                settings=settings,
                                cache_key=cache_key,
                                release_identifier=release_identifier,
                                release_date=release_date,
                                bbox=aoi_bbox,
                                aoi_geojson=normalized_aoi,
                                zoom=zoom,
                                summary=summary,
                                ttl_seconds=settings.wayback_metadata_cache_ttl_seconds,
                            )
                            write_wayback_metadata_cache_atomic(cache_path, payload)
                        except Exception as exc:
                            LOGGER.warning("Failed to write Wayback metadata cache for %s: %s", cache_key, exc)
            except Exception as exc:
                LOGGER.warning("Wayback metadata cache lookup failed for %s: %s", cache_key, exc)
                summary = None
                cache_hit = False
        if summary is None:
            summary = _live_lookup()
    except Exception as exc:
        _safe_add_timing_stage(
            timing,
            f"{stage_prefix}.metadata_lookup",
            duration_ms=_elapsed_ms(lookup_start),
            status="failed",
            metadata=_release_resolution_stage_metadata(
                scene_role=scene_role,
                release_id=release_identifier,
                zoom=zoom,
                min_zoom=min_zoom if min_zoom is not None else zoom,
                attempt_index=attempt_index,
                tilemap_preflight_enabled=tilemap_preflight_enabled,
                metadata_status="failed",
                exception_class=type(exc).__name__,
                cache_enabled=cache_enabled,
                cache_hit=cache_hit,
                cache_key=cache_key,
                cache_path_exists=cache_path_exists,
            ),
            error_type=type(exc).__name__,
        )
        raise
    _safe_add_timing_stage(
        timing,
        f"{stage_prefix}.metadata_lookup",
        duration_ms=_elapsed_ms(lookup_start),
        metadata=_release_resolution_stage_metadata(
            scene_role=scene_role,
            release_id=release_identifier,
            zoom=zoom,
            min_zoom=min_zoom if min_zoom is not None else zoom,
            attempt_index=attempt_index,
            tilemap_preflight_enabled=tilemap_preflight_enabled,
            metadata_status="usable" if _metadata_has_usable_coverage(summary) else "unusable",
            coverage_ok=_metadata_has_usable_coverage(summary),
            cache_enabled=cache_enabled,
            cache_hit=cache_hit,
            cache_key=cache_key,
            cache_path_exists=cache_path_exists,
        ),
    )
    assert summary is not None
    return summary


def _preflight_release_tile_availability_for_request(
    settings: Settings,
    *,
    release,
    aoi_bbox: dict[str, float],
    zoom: int,
    timing: StageTimingRecorder | None = None,
    stage_prefix: str = "release_resolution",
    scene_role: str = "single",
    attempt_index: int | None = None,
    min_zoom: int | None = None,
) -> TileAvailabilitySummary:
    cache_key = build_wayback_tile_preflight_cache_key(
        settings,
        release=release,
        bbox=aoi_bbox,
        aoi_geojson=None,
        zoom=zoom,
    )
    cache_path = get_wayback_tile_preflight_cache_path(settings, cache_key)
    cache_enabled = settings.wayback_tile_preflight_cache_enabled
    cache_path_exists = cache_path.exists()
    cache_hit = False
    tilemap: TileAvailabilitySummary | None = None

    def _live_preflight() -> TileAvailabilitySummary:
        session_setup_start = time.perf_counter_ns()
        session = build_session(settings)
        _safe_add_timing_stage(
            timing,
            f"{stage_prefix}.session_setup",
            duration_ms=_elapsed_ms(session_setup_start),
            metadata=_release_resolution_stage_metadata(
                scene_role=scene_role,
                release_id=release.identifier,
                zoom=zoom,
                min_zoom=min_zoom if min_zoom is not None else zoom,
                attempt_index=attempt_index,
                tilemap_preflight_enabled=True,
                cache_enabled=cache_enabled,
                cache_hit=cache_hit,
                cache_key=cache_key,
                cache_path_exists=cache_path_exists,
            ),
        )
        try:
            return preflight_wayback_tile_availability(
                session,
                release,
                aoi_bbox,
                zoom=zoom,
                max_workers=settings.wayback_metadata_workers,
            )
        finally:
            _close_session_if_possible(session)

    preflight_start = time.perf_counter_ns()
    try:
        if cache_enabled:
            try:
                with acquire_wayback_tile_preflight_cache_lock(cache_path):
                    cache_path_exists = cache_path.exists()
                    tilemap = read_wayback_tile_preflight_cache(
                        cache_path,
                        cache_key=cache_key,
                        ttl_seconds=settings.wayback_tile_preflight_cache_ttl_seconds,
                    )
                    cache_hit = tilemap is not None
                    if tilemap is None:
                        tilemap = _live_preflight()
                        try:
                            payload = build_wayback_tile_preflight_cache_payload(
                                settings=settings,
                                cache_key=cache_key,
                                release=release,
                                bbox=aoi_bbox,
                                aoi_geojson=None,
                                zoom=zoom,
                                tilemap=tilemap,
                                ttl_seconds=settings.wayback_tile_preflight_cache_ttl_seconds,
                            )
                            write_wayback_tile_preflight_cache_atomic(cache_path, payload)
                        except Exception as exc:
                            LOGGER.warning("Failed to write Wayback tile preflight cache for %s: %s", cache_key, exc)
            except Exception as exc:
                LOGGER.warning("Wayback tile preflight cache lookup failed for %s: %s", cache_key, exc)
                tilemap = None
                cache_hit = False
        if tilemap is None:
            tilemap = _live_preflight()
    except Exception as exc:
        _safe_add_timing_stage(
            timing,
            f"{stage_prefix}.tile_availability_preflight",
            duration_ms=_elapsed_ms(preflight_start),
            status="failed",
            metadata=_release_resolution_stage_metadata(
                scene_role=scene_role,
                release_id=release.identifier,
                zoom=zoom,
                min_zoom=min_zoom if min_zoom is not None else zoom,
                attempt_index=attempt_index,
                tilemap_preflight_enabled=True,
                preflight_status="failed",
                exception_class=type(exc).__name__,
                cache_enabled=cache_enabled,
                cache_hit=cache_hit,
                cache_key=cache_key,
                cache_path_exists=cache_path_exists,
            ),
            error_type=type(exc).__name__,
        )
        raise
    _safe_add_timing_stage(
        timing,
        f"{stage_prefix}.tile_availability_preflight",
        duration_ms=_elapsed_ms(preflight_start),
        metadata=_release_resolution_stage_metadata(
            scene_role=scene_role,
            release_id=release.identifier,
            zoom=zoom,
            min_zoom=min_zoom if min_zoom is not None else zoom,
            attempt_index=attempt_index,
            tilemap_preflight_enabled=True,
            preflight_status="complete" if tilemap.preflight_complete else "incomplete",
            coverage_ok=tilemap.available_count > 0,
            cache_enabled=cache_enabled,
            cache_hit=cache_hit,
            cache_key=cache_key,
            cache_path_exists=cache_path_exists,
        ),
    )
    assert tilemap is not None
    return tilemap


def _pair_summary_df(
    *,
    prepared: PreparedRequest,
    zoom_t1: int,
    zoom_t2: int,
    scene_t1_metadata: MetadataSummary,
    scene_t2_metadata: MetadataSummary,
    t1_tilemap: TileAvailabilitySummary | None = None,
    t2_tilemap: TileAvailabilitySummary | None = None,
    t1_identifier: str | None = None,
    t2_identifier: str | None = None,
    t1_release_date: str | None = None,
    t2_release_date: str | None = None,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "label": "t1",
                "identifier": t1_identifier or prepared.t1_release.identifier,
                "zoom": zoom_t1,
                "release_date": t1_release_date or str(prepared.t1_release.release_date),
                "provider": "esri_wayback",
                "source_type": "historical_release",
                "dominant_src_date": scene_t1_metadata.dominant_src_date,
                "dominant_src_res_m": scene_t1_metadata.dominant_src_res_m,
                "capture_date_count": scene_t1_metadata.capture_date_count,
                "mixed_capture_dates": scene_t1_metadata.mixed_capture_dates,
                "metadata_region_count": scene_t1_metadata.metadata_region_count,
                "metadata_coverage_fraction": scene_t1_metadata.metadata_coverage_fraction,
                "tilemap_candidate_count": t1_tilemap.candidate_count if t1_tilemap is not None else None,
                "tilemap_available_count": t1_tilemap.available_count if t1_tilemap is not None else None,
                "tilemap_missing_count": t1_tilemap.missing_count if t1_tilemap is not None else None,
                "tilemap_availability_fraction": t1_tilemap.availability_fraction if t1_tilemap is not None else None,
                "tile_count": prepared.tile_count_per_scene,
            },
            {
                "label": "t2",
                "identifier": t2_identifier or prepared.t2_release.identifier,
                "zoom": zoom_t2,
                "release_date": t2_release_date or str(prepared.t2_release.release_date),
                "provider": "mapbox" if (t2_identifier or prepared.t2_release.identifier) == MAPBOX_SOURCE_ID else "esri_wayback",
                "source_type": "current_basemap" if (t2_identifier or prepared.t2_release.identifier) == MAPBOX_SOURCE_ID else "historical_release",
                "dominant_src_date": scene_t2_metadata.dominant_src_date,
                "dominant_src_res_m": scene_t2_metadata.dominant_src_res_m,
                "capture_date_count": scene_t2_metadata.capture_date_count,
                "mixed_capture_dates": scene_t2_metadata.mixed_capture_dates,
                "metadata_region_count": scene_t2_metadata.metadata_region_count,
                "metadata_coverage_fraction": scene_t2_metadata.metadata_coverage_fraction,
                "tilemap_candidate_count": t2_tilemap.candidate_count if t2_tilemap is not None else None,
                "tilemap_available_count": t2_tilemap.available_count if t2_tilemap is not None else None,
                "tilemap_missing_count": t2_tilemap.missing_count if t2_tilemap is not None else None,
                "tilemap_availability_fraction": t2_tilemap.availability_fraction if t2_tilemap is not None else None,
                "tile_count": prepared.tile_count_per_scene,
            },
        ]
    )


def _segmentation_summary_df(
    *,
    release_identifier: str,
    release_date: str,
    zoom: int,
    scene_metadata: MetadataSummary,
    tilemap: TileAvailabilitySummary | None,
    tile_count: int,
    prompt: str | None,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "label": "source",
                "identifier": release_identifier,
                "zoom": zoom,
                "release_date": release_date,
                "dominant_src_date": scene_metadata.dominant_src_date,
                "dominant_src_res_m": scene_metadata.dominant_src_res_m,
                "capture_date_count": scene_metadata.capture_date_count,
                "mixed_capture_dates": scene_metadata.mixed_capture_dates,
                "metadata_region_count": scene_metadata.metadata_region_count,
                "metadata_coverage_fraction": scene_metadata.metadata_coverage_fraction,
                "tilemap_candidate_count": tilemap.candidate_count if tilemap is not None else None,
                "tilemap_available_count": tilemap.available_count if tilemap is not None else None,
                "tilemap_missing_count": tilemap.missing_count if tilemap is not None else None,
                "tilemap_availability_fraction": tilemap.availability_fraction if tilemap is not None else None,
                "tile_count": tile_count,
                "prompt": prompt,
            }
        ]
    )


def _metadata_has_usable_coverage(metadata: MetadataSummary) -> bool:
    return (
        metadata.metadata_region_count > 0
        or metadata.dominant_src_date is not None
        or (metadata.metadata_coverage_fraction is not None and metadata.metadata_coverage_fraction > 0.0)
    )


def _resolve_release_for_aoi(
    settings: Settings,
    *,
    release,
    aoi_bbox: dict[str, float],
    normalized_aoi: dict[str, Any],
    timing: StageTimingRecorder | None = None,
    stage_prefix: str = "release_resolution",
    scene_role: str = "single",
) -> ResolvedWaybackRelease:
    total_start = time.perf_counter_ns()
    last_metadata: MetadataSummary | None = None
    last_tilemap: TileAvailabilitySummary | None = None
    attempted_zooms: list[int] = []
    attempt_count = 0
    preferred_zoom = settings.wayback_preferred_inference_zoom
    selected_zoom = preferred_zoom
    selected_release = release.identifier
    fallback_used = True
    coverage_ok = False
    total_status: str = "success"
    total_error_type: str | None = None
    decision_recorded = False

    try:
        for attempt_index, zoom in enumerate(range(preferred_zoom, settings.min_zoom - 1, -1), start=1):
            attempt_count = attempt_index
            attempted_zooms.append(zoom)
            attempt_start = time.perf_counter_ns()
            metadata = _summarize_release_metadata_for_request(
                settings,
                release_identifier=release.identifier,
                release_date=str(release.release_date),
                aoi_bbox=aoi_bbox,
                normalized_aoi=normalized_aoi,
                zoom=zoom,
                timing=timing,
                stage_prefix=stage_prefix,
                scene_role=scene_role,
                attempt_index=attempt_index,
                min_zoom=settings.min_zoom,
                tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
            )
            tilemap = (
                _preflight_release_tile_availability_for_request(
                    settings,
                    release=release,
                    aoi_bbox=aoi_bbox,
                    zoom=zoom,
                    timing=timing,
                    stage_prefix=stage_prefix,
                    scene_role=scene_role,
                    attempt_index=attempt_index,
                    min_zoom=settings.min_zoom,
                )
                if settings.wayback_tilemap_preflight_enabled
                else None
            )
            last_metadata = metadata
            last_tilemap = tilemap

            if tilemap is not None:
                coverage_ok = tilemap.available_count > 0 or (
                    tilemap.failed_check_count > 0 and _metadata_has_usable_coverage(metadata)
                )
                metadata_status = "usable" if _metadata_has_usable_coverage(metadata) else "unusable"
                preflight_status = "complete" if tilemap.preflight_complete else "incomplete"
                _safe_add_timing_stage(
                    timing,
                    f"{stage_prefix}.zoom_attempt",
                    duration_ms=_elapsed_ms(attempt_start),
                    metadata=_release_resolution_stage_metadata(
                        scene_role=scene_role,
                        release_id=release.identifier,
                        zoom=zoom,
                        min_zoom=settings.min_zoom,
                        attempt_index=attempt_index,
                        tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
                        metadata_status=metadata_status,
                        preflight_status=preflight_status,
                        coverage_ok=coverage_ok,
                    ),
                )
                if coverage_ok:
                    selected_zoom = zoom
                    fallback_used = False
                    decision_start = time.perf_counter_ns()
                    _safe_add_timing_stage(
                        timing,
                        f"{stage_prefix}.decision",
                        duration_ms=_elapsed_ms(decision_start),
                        metadata=_release_resolution_stage_metadata(
                            scene_role=scene_role,
                            release_id=release.identifier,
                            zoom=zoom,
                            min_zoom=settings.min_zoom,
                            attempt_index=attempt_index,
                            tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
                            metadata_status=metadata_status,
                            preflight_status=preflight_status,
                            coverage_ok=True,
                            fallback_used=fallback_used,
                            zoom_fallback_applied=zoom < preferred_zoom,
                            selected_zoom=zoom,
                            selected_release=release.identifier,
                        ),
                    )
                    decision_recorded = True
                    return ResolvedWaybackRelease(release=release, zoom=zoom, metadata=metadata, tilemap=tilemap)
                continue

            coverage_ok = _metadata_has_usable_coverage(metadata)
            metadata_status = "usable" if coverage_ok else "unusable"
            _safe_add_timing_stage(
                timing,
                f"{stage_prefix}.zoom_attempt",
                duration_ms=_elapsed_ms(attempt_start),
                metadata=_release_resolution_stage_metadata(
                    scene_role=scene_role,
                    release_id=release.identifier,
                    zoom=zoom,
                    min_zoom=settings.min_zoom,
                    attempt_index=attempt_index,
                    tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
                    metadata_status=metadata_status,
                    preflight_status="skipped",
                    coverage_ok=coverage_ok,
                ),
            )
            if coverage_ok:
                selected_zoom = zoom
                fallback_used = False
                decision_start = time.perf_counter_ns()
                _safe_add_timing_stage(
                    timing,
                    f"{stage_prefix}.decision",
                    duration_ms=_elapsed_ms(decision_start),
                    metadata=_release_resolution_stage_metadata(
                        scene_role=scene_role,
                        release_id=release.identifier,
                        zoom=zoom,
                        min_zoom=settings.min_zoom,
                        attempt_index=attempt_index,
                        tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
                        metadata_status=metadata_status,
                        preflight_status="skipped",
                        coverage_ok=True,
                        fallback_used=fallback_used,
                        zoom_fallback_applied=zoom < preferred_zoom,
                        selected_zoom=zoom,
                        selected_release=release.identifier,
                    ),
                )
                decision_recorded = True
                return ResolvedWaybackRelease(release=release, zoom=zoom, metadata=metadata, tilemap=None)

        selected_zoom = preferred_zoom
        fallback_used = True
        coverage_ok = _metadata_has_usable_coverage(last_metadata or MetadataSummary(dominant_src_date=None, dominant_src_res_m=None))
        decision_start = time.perf_counter_ns()
        _safe_add_timing_stage(
            timing,
            f"{stage_prefix}.decision",
            duration_ms=_elapsed_ms(decision_start),
            metadata=_release_resolution_stage_metadata(
                scene_role=scene_role,
                release_id=release.identifier,
                zoom=selected_zoom,
                min_zoom=settings.min_zoom,
                attempt_index=attempt_count if attempt_count else None,
                tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
                metadata_status="usable" if coverage_ok else "unusable",
                preflight_status="skipped" if last_tilemap is None else ("complete" if last_tilemap.preflight_complete else "incomplete"),
                coverage_ok=coverage_ok,
                fallback_used=True,
                selected_zoom=selected_zoom,
                selected_release=selected_release,
            ),
        )
        decision_recorded = True
        return ResolvedWaybackRelease(
            release=release,
            zoom=settings.zoom,
            metadata=last_metadata or MetadataSummary(dominant_src_date=None, dominant_src_res_m=None),
            tilemap=last_tilemap,
        )
    except Exception as exc:
        total_status = "failed"
        total_error_type = type(exc).__name__
        raise
    finally:
        total_metadata = _release_resolution_stage_metadata(
            scene_role=scene_role,
            release_id=release.identifier,
            zoom=selected_zoom,
            min_zoom=settings.min_zoom,
            attempt_index=attempt_count if attempt_count else None,
            tilemap_preflight_enabled=settings.wayback_tilemap_preflight_enabled,
            coverage_ok=coverage_ok,
            fallback_used=fallback_used,
            selected_zoom=selected_zoom,
            selected_release=selected_release,
            exception_class=total_error_type,
        )
        total_metadata["attempt_count"] = attempt_count
        total_metadata["attempted_zooms"] = attempted_zooms
        total_metadata["decision_recorded"] = decision_recorded
        _safe_add_timing_stage(
            timing,
            f"{stage_prefix}.total",
            duration_ms=_elapsed_ms(total_start),
            status=total_status,
            metadata=total_metadata,
            error_type=total_error_type,
        )


def _coverage_entry(
    release_identifier: str,
    zoom: int,
    metadata: MetadataSummary,
    tilemap: TileAvailabilitySummary | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "release_identifier": release_identifier,
        "zoom": zoom,
        "metadata_region_count": metadata.metadata_region_count,
        "capture_date_count": metadata.capture_date_count,
        "mixed_capture_dates": metadata.mixed_capture_dates,
        "dominant_src_date": metadata.dominant_src_date,
        "dominant_src_res_m": metadata.dominant_src_res_m,
        "metadata_coverage_fraction": metadata.metadata_coverage_fraction,
    }
    if tilemap is not None:
        entry.update(tilemap.to_dict())
    return entry


def _build_failure_diagnostics(
    *,
    timings: StageTimings,
    prepared: PreparedRequest,
    request: RunRequest,
    min_new_building_pixels: int,
    old_building_mask_dilation_pixels: int,
    new_building_core_distance_pixels: int,
    scene_t1_metadata: MetadataSummary | None = None,
    scene_t2_metadata: MetadataSummary | None = None,
    tilemap_t1: TileAvailabilitySummary | None = None,
    tilemap_t2: TileAvailabilitySummary | None = None,
    zoom_t1: int | None = None,
    zoom_t2: int | None = None,
    warnings: list[str] | None = None,
    backend: dict[str, Any] | None = None,
) -> DiagnosticMetadata:
    coverage: dict[str, Any] = {}
    if scene_t1_metadata is not None:
        coverage["t1"] = _coverage_entry(prepared.t1_release.identifier, zoom_t1 or 0, scene_t1_metadata, tilemap_t1)
    if scene_t2_metadata is not None:
        coverage["t2"] = _coverage_entry(prepared.t2_release.identifier, zoom_t2 or 0, scene_t2_metadata, tilemap_t2)
    return DiagnosticMetadata(
        cache_hit=False,
        stage_seconds=dict(timings.values),
        tile_counts={
            "t1": tilemap_t1.candidate_count if tilemap_t1 is not None else prepared.tile_count_per_scene,
            "t2": tilemap_t2.candidate_count if tilemap_t2 is not None else prepared.tile_count_per_scene,
            "total": (
                (tilemap_t1.candidate_count if tilemap_t1 is not None else prepared.tile_count_per_scene)
                + (tilemap_t2.candidate_count if tilemap_t2 is not None else prepared.tile_count_per_scene)
            ),
        },
        thresholds={
            "change_threshold": request.change_threshold,
            "semantic_threshold": request.semantic_threshold,
            "old_building_mask_dilation_pixels": float(old_building_mask_dilation_pixels),
            "new_building_core_distance_pixels": float(new_building_core_distance_pixels),
        },
        min_new_building_pixels=min_new_building_pixels,
        backend=backend or {},
        warnings=warnings or [],
        coverage=coverage,
    )


def _tilemap_unavailability_message(
    *,
    release_identifier: str,
    metadata: MetadataSummary,
    tilemap: TileAvailabilitySummary,
    zoom: int,
) -> str:
    if metadata.metadata_region_count == 0 and metadata.dominant_src_date is None:
        return (
            f"Selected Wayback release {release_identifier} has no metadata coverage or downloadable imagery "
            f"for the requested AOI at z={zoom}."
        )
    return (
        f"Selected Wayback release {release_identifier} has metadata coverage but zero downloadable WMTS tiles "
        f"for the requested AOI at z={zoom}."
    )


def run_detection(
    request: RunRequest,
    *,
    settings: Settings,
    progress: ProgressReporter = None,
    x_ip_token: str | None = None,
    inference_runner=None,
    model_backend: Literal["bandon_mps"] = "bandon_mps",
    remote_patch_budget_enabled: bool = False,
    request_hash_context: dict[str, object] | None = None,
) -> RunResponse:
    request_hash_context = {
        **(request_hash_context or {}),
        "imagery_source_recipe": "canonical_cog_inference_v1",
    }
    validation_started_ns = time.perf_counter_ns()
    releases = list_releases(settings)
    validation, prepared = validate_request(
        request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=remote_patch_budget_enabled,
        request_hash_context=request_hash_context,
    )
    if not validation.valid or prepared is None:
        return RunResponse(
            success=False,
            error_code="invalid_request",
            error_message="; ".join(validation.blocking_errors) or "The request is invalid.",
        )

    validation_duration_ms = _elapsed_ms(validation_started_ns)
    timing = StageTimingRecorder(
        run_id=prepared.request_hash,
        pipeline_kind="detection",
        metadata={"mode": request.mode, "model_backend": model_backend},
    )
    timing.add_stage(
        "validation",
        duration_ms=validation_duration_ms,
        metadata={"valid": True, "estimated_tiles": prepared.tile_count_per_scene * 2},
    )
    if settings.inference_backend == "mtgcdnet_s2looking_mps":
        change_threshold = float(settings.default_change_threshold)
    else:
        change_threshold = float(request.change_threshold if request.change_threshold is not None else settings.default_change_threshold)
    semantic_threshold = float(request.semantic_threshold if request.semantic_threshold is not None else settings.default_semantic_threshold)
    old_building_mask_dilation_pixels = max(
        int(
            request.old_building_mask_dilation_pixels
            if request.old_building_mask_dilation_pixels is not None
            else settings.default_old_building_mask_dilation_pixels
        ),
        0,
    )
    new_building_core_distance_pixels = max(
        int(
            request.new_building_core_distance_pixels
            if request.new_building_core_distance_pixels is not None
            else settings.default_new_building_core_distance_pixels
        ),
        0,
    )
    run_identity = _detection_run_identity(
        settings=settings,
        model_backend=model_backend,
        request_hash=prepared.request_hash,
        change_threshold=change_threshold,
        request_hash_context=request_hash_context,
    )
    _log_effective_detection_identity(run_identity)

    with timing.stage("imagery_cache_lookup"):
        cached = load_cached_response(settings, prepared.request_hash)
    if cached is not None:
        if cached.diagnostics is not None:
            cached.diagnostics.cache_hit = True
        result_dir = request_result_dir(settings, prepared.request_hash)
        _write_timing_report_safely(timing, result_dir)
        return cached

    session = build_session(settings)
    timings = StageTimings(recorder=timing, stage_name_map=DETECTION_STAGE_MAP)
    result_dir = request_result_dir(settings, prepared.request_hash)
    run_tmp_dir = get_run_tmp_dir(settings, prepared.request_hash)
    run_succeeded = False

    from src.utils.geometry import bounds_dict, parse_aoi_geometry

    geometry = parse_aoi_geometry(prepared.normalized_aoi)
    aoi_bbox = bounds_dict(geometry)
    min_pixels = resolve_min_new_building_pixels(
        request,
        normalized_aoi=prepared.normalized_aoi,
        settings=settings,
    )
    run_warnings: list[str] = []
    backend_diagnostics: dict[str, Any] = {
        "model_backend": model_backend,
        "effective_backend": settings.inference_backend,
    }
    if model_backend == "bandon_mps":
        backend_diagnostics.update(
            {
                "repo_dir": str(settings.bandon_repo_dir),
                "env_prefix": str(settings.bandon_env_prefix),
                "config_path": str(settings.bandon_config_path),
                "checkpoint_path": str(settings.bandon_checkpoint_path),
                "device_requested": settings.bandon_device,
                "allow_mps_fallback": settings.bandon_allow_mps_fallback,
            }
        )

    LOGGER.info("Running detection for request %s", prepared.request_hash)
    try:
        _report(progress, 0.05, "Resolving Wayback metadata")
        if settings.wayback_tilemap_preflight_enabled:
            _report(progress, 0.1, "Checking tile availability")

        parent_timing = timings.recorder
        t1_timing = (
            StageTimingRecorder(
                run_id=f"{parent_timing.run_id}:t1",
                pipeline_kind=parent_timing.pipeline_kind,
                project_id=parent_timing.project_id,
                metadata=parent_timing.metadata,
            )
            if parent_timing is not None
            else None
        )
        t2_timing = (
            StageTimingRecorder(
                run_id=f"{parent_timing.run_id}:t2",
                pipeline_kind=parent_timing.pipeline_kind,
                project_id=parent_timing.project_id,
                metadata=parent_timing.metadata,
            )
            if parent_timing is not None
            else None
        )

        use_mapbox_t2 = prepared.latest_source == "mapbox_current"
        with timings.track("release_resolution"):
            if use_mapbox_t2:
                resolved_t1 = _resolve_release_for_aoi(
                    settings,
                    release=prepared.t1_release,
                    aoi_bbox=aoi_bbox,
                    normalized_aoi=prepared.normalized_aoi,
                    timing=t1_timing,
                    stage_prefix="release_resolution.t1",
                    scene_role="t1",
                )
                resolved_t2 = ResolvedWaybackRelease(
                    release=prepared.t2_release,
                    zoom=min(settings.mapbox_current_imagery_default_zoom, settings.mapbox_current_imagery_max_zoom),
                    metadata=MetadataSummary(dominant_src_date=None, dominant_src_res_m=None),
                    tilemap=None,
                )
                _safe_add_timing_stage(
                    t2_timing,
                    "release_resolution.t2.total",
                    duration_ms=0.0,
                    metadata={
                        "scene_role": "t2",
                        "provider": "mapbox",
                        "source_type": "current_basemap",
                        "source_id": MAPBOX_SOURCE_ID,
                        "capture_date_known": False,
                    },
                )
            else:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    future_t1_resolution = executor.submit(
                        _resolve_release_for_aoi,
                        settings,
                        release=prepared.t1_release,
                        aoi_bbox=aoi_bbox,
                        normalized_aoi=prepared.normalized_aoi,
                        timing=t1_timing,
                        stage_prefix="release_resolution.t1",
                        scene_role="t1",
                    )
                    future_t2_resolution = executor.submit(
                        _resolve_release_for_aoi,
                        settings,
                        release=prepared.t2_release,
                        aoi_bbox=aoi_bbox,
                        normalized_aoi=prepared.normalized_aoi,
                        timing=t2_timing,
                        stage_prefix="release_resolution.t2",
                        scene_role="t2",
                    )
                    resolved_t1 = future_t1_resolution.result()
                    resolved_t2 = future_t2_resolution.result()

        if parent_timing is not None and t1_timing is not None and t2_timing is not None:
            parent_timing.merge_child_timings(t1_timing)
            parent_timing.merge_child_timings(t2_timing)

        scene_t1_metadata = resolved_t1.metadata
        scene_t2_metadata = resolved_t2.metadata
        tilemap_t1 = resolved_t1.tilemap
        tilemap_t2 = resolved_t2.tilemap
        for label, resolved in (("T1", resolved_t1), ("T2", resolved_t2)):
            if label == "T2" and use_mapbox_t2:
                continue
            if resolved.zoom < settings.wayback_preferred_inference_zoom:
                run_warnings.append(
                    f"{label} release {resolved.release.identifier} is being downloaded at z={resolved.zoom} because z={settings.wayback_preferred_inference_zoom} has no safe AOI coverage."
                )

        for label, release_identifier, metadata, tilemap in (
            ("T1", prepared.t1_release.identifier, scene_t1_metadata, tilemap_t1),
            ("T2", MAPBOX_SOURCE_ID if use_mapbox_t2 else prepared.t2_release.identifier, scene_t2_metadata, tilemap_t2),
        ):
            if metadata.mixed_capture_dates:
                run_warnings.append(
                    f"{label} release {release_identifier} intersects {metadata.capture_date_count} capture-date regions within the AOI."
                )
            if tilemap is not None:
                if tilemap.failed_check_count > 0:
                    run_warnings.append(
                        f"{label} tile availability preflight was incomplete: {tilemap.failed_check_count} tile checks failed."
                    )
                elif tilemap.available_count < tilemap.candidate_count:
                    resolved_zoom = resolved_t1.zoom if label == "T1" else resolved_t2.zoom
                    run_warnings.append(
                        f"{label} release {release_identifier} has partial z={resolved_zoom} tile coverage for the AOI "
                        f"({tilemap.available_count}/{tilemap.candidate_count} tiles available)."
                    )

        with timings.track("tile_indexing"):
            pair_summary_df = _pair_summary_df(
                prepared=prepared,
                zoom_t1=resolved_t1.zoom,
                zoom_t2=resolved_t2.zoom,
                scene_t1_metadata=scene_t1_metadata,
                scene_t2_metadata=scene_t2_metadata,
                t1_tilemap=tilemap_t1,
                t2_tilemap=tilemap_t2,
                t2_identifier=MAPBOX_SOURCE_ID if use_mapbox_t2 else None,
                t2_release_date="current_basemap" if use_mapbox_t2 else None,
            )

        for tilemap, release_identifier, metadata, resolved_zoom in (
            (tilemap_t1, prepared.t1_release.identifier, scene_t1_metadata, resolved_t1.zoom),
            (tilemap_t2, prepared.t2_release.identifier, scene_t2_metadata, resolved_t2.zoom),
        ):
            if use_mapbox_t2 and release_identifier == prepared.t2_release.identifier:
                continue
            if tilemap is not None and tilemap.preflight_complete and tilemap.available_count == 0:
                return RunResponse(
                    success=False,
                    error_code="wayback_tile_coverage_unavailable",
                    error_message=_tilemap_unavailability_message(
                        release_identifier=release_identifier,
                        metadata=metadata,
                        tilemap=tilemap,
                        zoom=resolved_zoom,
                    ),
                    diagnostics=_build_failure_diagnostics(
                        timings=timings,
                        prepared=prepared,
                        request=request,
                        min_new_building_pixels=min_pixels,
                        old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                        new_building_core_distance_pixels=new_building_core_distance_pixels,
                        scene_t1_metadata=scene_t1_metadata,
                        scene_t2_metadata=scene_t2_metadata,
                        tilemap_t1=tilemap_t1,
                        tilemap_t2=tilemap_t2,
                        zoom_t1=resolved_t1.zoom,
                        zoom_t2=resolved_t2.zoom,
                        warnings=run_warnings,
                        backend=backend_diagnostics,
                    ),
                )

        _report(progress, 0.18, "Downloading imagery" if use_mapbox_t2 else "Downloading Wayback imagery")
        try:
            with timings.track("download"):
                selected_tiles_t1 = _resolve_available_tiles_for_aoi(
                    prepared.normalized_aoi,
                    bbox=aoi_bbox,
                    zoom=resolved_t1.zoom,
                    preflight_available_tiles=tilemap_t1.available_tiles
                    if tilemap_t1 is not None and tilemap_t1.preflight_complete
                    else None,
                )
                scene_t1 = get_or_create_inference_reference_imagery(
                    release=prepared.t1_release,
                    normalized_aoi=prepared.normalized_aoi,
                    bbox=aoi_bbox,
                    settings=settings,
                    zoom=resolved_t1.zoom,
                    out_dir=result_dir,
                    available_tiles=selected_tiles_t1,
                    source_role="t1",
                    progress_callback=lambda payload: _report(
                        progress,
                        0.18,
                        _wayback_download_progress_message(payload),
                        payload,
                    ),
                )
                if use_mapbox_t2:
                    mapbox_start = time.perf_counter_ns()
                    scene_t2 = MapboxCurrentProvider().download(
                        aoi_bbox,
                        settings=settings,
                        zoom=resolved_t2.zoom,
                        aoi_geojson=prepared.normalized_aoi,
                    )
                    _safe_add_timing_stage(
                        timing,
                        "mapbox_imagery_download_or_load",
                        duration_ms=_elapsed_ms(mapbox_start),
                        metadata={
                            "provider": "mapbox",
                            "cache_hit": bool((scene_t2.metadata or {}).get("cache_hit")),
                            "tile_count": scene_t2.tile_count,
                            "zoom": scene_t2.zoom,
                        },
                    )
                    run_warnings.append(
                        "The latest milestone uses Mapbox Satellite current basemap imagery. Exact capture date is not guaranteed."
                    )
                    backend_diagnostics["latest_source"] = {
                        "provider": "mapbox",
                        "source_type": "current_basemap",
                        "source_id": MAPBOX_SOURCE_ID,
                        "capture_date_known": False,
                        "attribution": MAPBOX_ATTRIBUTION,
                    }
                else:
                    selected_tiles_t2 = _resolve_available_tiles_for_aoi(
                        prepared.normalized_aoi,
                        bbox=aoi_bbox,
                        zoom=resolved_t2.zoom,
                        preflight_available_tiles=tilemap_t2.available_tiles
                        if tilemap_t2 is not None and tilemap_t2.preflight_complete
                        else None,
                    )
                    scene_t2 = get_or_create_inference_reference_imagery(
                        release=prepared.t2_release,
                        normalized_aoi=prepared.normalized_aoi,
                        bbox=aoi_bbox,
                        settings=settings,
                        zoom=resolved_t2.zoom,
                        out_dir=result_dir,
                        available_tiles=selected_tiles_t2,
                        source_role="t2",
                        progress_callback=lambda payload: _report(
                            progress,
                            0.18,
                            _wayback_download_progress_message(payload),
                            payload,
                        ),
                    )
        except WaybackTileDownloadError as exc:
            backend_diagnostics["wayback_download_error"] = exc.details
            return RunResponse(
                success=False,
                error_code="wayback_tile_download_failed",
                error_message=str(exc),
                diagnostics=_build_failure_diagnostics(
                    timings=timings,
                    prepared=prepared,
                    request=request,
                    min_new_building_pixels=min_pixels,
                    old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                    new_building_core_distance_pixels=new_building_core_distance_pixels,
                    scene_t1_metadata=scene_t1_metadata,
                    scene_t2_metadata=scene_t2_metadata,
                    tilemap_t1=tilemap_t1,
                    tilemap_t2=tilemap_t2,
                    zoom_t1=resolved_t1.zoom,
                    zoom_t2=resolved_t2.zoom,
                    warnings=run_warnings,
                    backend=backend_diagnostics,
                ),
            )
        except ValueError as exc:
            return RunResponse(
                success=False,
                error_code="wayback_tile_coverage_unavailable",
                error_message=str(exc),
                diagnostics=_build_failure_diagnostics(
                    timings=timings,
                    prepared=prepared,
                    request=request,
                    min_new_building_pixels=min_pixels,
                    old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                    new_building_core_distance_pixels=new_building_core_distance_pixels,
                    scene_t1_metadata=scene_t1_metadata,
                    scene_t2_metadata=scene_t2_metadata,
                    tilemap_t1=tilemap_t1,
                    tilemap_t2=tilemap_t2,
                    zoom_t1=resolved_t1.zoom,
                    zoom_t2=resolved_t2.zoom,
                    warnings=run_warnings,
                    backend=backend_diagnostics,
                ),
            )
        except requests.RequestException as exc:
            return RunResponse(
                success=False,
                error_code="wayback_tile_download_failed",
                error_message=f"{type(exc).__name__}: {exc}",
                diagnostics=_build_failure_diagnostics(
                    timings=timings,
                    prepared=prepared,
                    request=request,
                    min_new_building_pixels=min_pixels,
                    old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                    new_building_core_distance_pixels=new_building_core_distance_pixels,
                    scene_t1_metadata=scene_t1_metadata,
                    scene_t2_metadata=scene_t2_metadata,
                    tilemap_t1=tilemap_t1,
                    tilemap_t2=tilemap_t2,
                    zoom_t1=resolved_t1.zoom,
                    zoom_t2=resolved_t2.zoom,
                    warnings=run_warnings,
                    backend=backend_diagnostics,
                ),
            )

        imagery_source_diagnostics = {
            "t1": {
                "imagery_source_mode": (scene_t1.metadata or {}).get("imagery_source_mode"),
                "reference_imagery_key": (scene_t1.metadata or {}).get("reference_imagery_key"),
                "canonical_cog_path": (scene_t1.metadata or {}).get("canonical_cog_path"),
                "valid_mask_source": (scene_t1.metadata or {}).get("valid_mask_source"),
                "fallback_reason": (scene_t1.metadata or {}).get("fallback_reason"),
                "canonical_cog_validation": (scene_t1.metadata or {}).get("canonical_cog_validation"),
            },
            "t2": {
                "imagery_source_mode": (scene_t2.metadata or {}).get("imagery_source_mode"),
                "reference_imagery_key": (scene_t2.metadata or {}).get("reference_imagery_key"),
                "canonical_cog_path": (scene_t2.metadata or {}).get("canonical_cog_path"),
                "valid_mask_source": (scene_t2.metadata or {}).get("valid_mask_source"),
                "fallback_reason": (scene_t2.metadata or {}).get("fallback_reason"),
                "canonical_cog_validation": (scene_t2.metadata or {}).get("canonical_cog_validation"),
            },
        }
        backend_diagnostics["imagery_sources"] = imagery_source_diagnostics

        with rasterio.open(scene_t2.geotiff_path) as _tiled_probe_src:
            inference_decision = select_inference_mode(
                width=_tiled_probe_src.width,
                height=_tiled_probe_src.height,
                tile_count=max(scene_t1.tile_count, scene_t2.tile_count),
                settings=settings,
            )
        backend_diagnostics["inference_mode"] = inference_decision.__dict__
        if model_backend == "bandon_mps" and inference_decision.mode == "tiled":
            _report(
                progress,
                0.45,
                "Running tiled local MTGCDNet change detection",
                {
                    "stage": "tiled_inference",
                    "mode": "tiled",
                    "reason": inference_decision.reason,
                    "tile_size": settings.inference_tile_size,
                    "tile_overlap": settings.inference_tile_overlap,
                    "width": inference_decision.width,
                    "height": inference_decision.height,
                    "pixel_count": inference_decision.pixel_count,
                },
            )
            try:
                with timings.track("bandon_inference", mode="tiled", reason=inference_decision.reason):
                    tiled_result = run_tiled_inference(
                        t1_mosaic_path=scene_t1.geotiff_path,
                        t2_mosaic_path=scene_t2.geotiff_path,
                        t1_valid_mask_path=scene_t1.valid_mask_path,
                        t2_valid_mask_path=scene_t2.valid_mask_path,
                        output_dir=result_dir,
                        run_id=prepared.request_hash,
                        settings=settings,
                        config=TiledInferenceConfig.from_settings(settings, threshold=change_threshold),
                        predictor=make_bandon_patch_predictor(
                            settings=settings,
                            effective_backend=settings.inference_backend,
                            threshold=change_threshold,
                        ),
                        aoi_geojson=prepared.normalized_aoi,
                        release_t1=prepared.t1_release.identifier,
                        release_t2=MAPBOX_SOURCE_ID if use_mapbox_t2 else prepared.t2_release.identifier,
                        progress_callback=lambda payload: _report(
                            progress,
                            min(
                                0.90,
                                0.45
                                + 0.45
                                * (
                                    float(payload.get("processed_tiles") or 0)
                                    / max(float(payload.get("total_tiles") or 1), 1.0)
                                ),
                            ),
                            "Running tiled local MTGCDNet change detection",
                            payload,
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                return RunResponse(
                    success=False,
                    error_code="tiled_inference_failed",
                    error_message=f"{type(exc).__name__}: {exc}",
                    diagnostics=_build_failure_diagnostics(
                        timings=timings,
                        prepared=prepared,
                        request=request,
                        min_new_building_pixels=min_pixels,
                        old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                        new_building_core_distance_pixels=new_building_core_distance_pixels,
                        scene_t1_metadata=scene_t1_metadata,
                        scene_t2_metadata=scene_t2_metadata,
                        tilemap_t1=tilemap_t1,
                        tilemap_t2=tilemap_t2,
                        zoom_t1=resolved_t1.zoom,
                        zoom_t2=resolved_t2.zoom,
                        warnings=run_warnings,
                        backend=backend_diagnostics,
                    ),
                )

            _report(progress, 0.92, "Exporting tiled artifacts")
            max_response_features = max(0, int(settings.reference_layer_browser_geojson_max_features))
            change_polygons_geojson, total_feature_count, response_geojson_capped = _feature_collection_from_geojsonl(
                tiled_result.geojsonl_path,
                max_features=max_response_features,
            )
            if response_geojson_capped:
                run_warnings.append(
                    f"Tiled response GeoJSON was capped at {max_response_features} features; full output is available as GeoJSONL artifact."
                )
            change_polygons_path = write_geojson(result_dir / "building_change_polygons.geojson", change_polygons_geojson)
            summary_csv = _write_tiled_summary_csv(
                result_dir / "wayback_pair_summary.csv",
                pair_summary_df.to_dict(orient="records"),
            )
            artifacts = [
                ArtifactEntry(
                    name="prediction_change_probability_tif",
                    path=str(tiled_result.probability_path),
                    media_type="image/tiff",
                    description="Tiled change probability raster",
                ),
                ArtifactEntry(
                    name="prediction_change_mask_tif",
                    path=str(tiled_result.mask_path),
                    media_type="image/tiff",
                    description="Tiled building-change mask raster",
                ),
                ArtifactEntry(
                    name="building_change_polygons_geojsonl",
                    path=str(tiled_result.geojsonl_path),
                    media_type="application/x-ndjson",
                    description="Streaming tiled building-change polygons",
                ),
                ArtifactEntry(
                    name="building_change_polygons_geojson",
                    path=str(change_polygons_path),
                    media_type="application/geo+json",
                    description="Building-change polygons for browser display",
                ),
                ArtifactEntry(
                    name="tiled_inference_metadata_json",
                    path=str(tiled_result.metadata_path),
                    media_type="application/json",
                    description="Tiled inference metadata and progress summary",
                ),
                ArtifactEntry(
                    name="wayback_pair_summary_csv",
                    path=str(summary_csv),
                    media_type="text/csv",
                    description="Wayback pair summary",
                ),
            ]
            backend_diagnostics["bandon"] = {
                "effective_backend": settings.inference_backend,
                "runner_family": "bandon_mps",
                "checkpoint_path": run_identity.get("checkpoint_path"),
                "checkpoint_sha256": run_identity.get("checkpoint_sha256"),
                "threshold": change_threshold,
                "config_path": run_identity.get("config_path"),
                "mode": "tiled",
                "tiled_inference": tiled_result.metadata,
            }
            tabular_metrics = TabularMetrics(
                summary_rows=pair_summary_df.to_dict(orient="records"),
                change_rows=[feature.get("properties", {}) for feature in change_polygons_geojson.get("features", [])],
            )
            total_change_area = float(
                sum(
                    float((feature.get("properties") or {}).get("area_m2") or 0.0)
                    for feature in change_polygons_geojson.get("features", [])
                )
            )
            response = RunResponse(
                success=True,
                summary=SummaryStats(
                    request_hash=prepared.request_hash,
                    mode=request.mode,
                    model_backend=model_backend,
                    result_semantics="building_change",
                    estimated_area_m2=round(prepared.area_m2, 2),
                    tile_count_t1=scene_t1.tile_count,
                    tile_count_t2=scene_t2.tile_count,
                    total_new_buildings=0,
                    total_building_blocks=0,
                    total_new_building_area_m2=0.0,
                    total_building_block_area_m2=0.0,
                    total_change_polygons=int(total_feature_count),
                    total_change_area_m2=round(total_change_area, 2),
                    release_date_t1=str(prepared.t1_release.release_date),
                    release_date_t2="current_basemap" if use_mapbox_t2 else str(prepared.t2_release.release_date),
                    dominant_src_date_t1=scene_t1_metadata.dominant_src_date,
                    dominant_src_date_t2=scene_t2_metadata.dominant_src_date,
                    dominant_src_res_m_t1=scene_t1_metadata.dominant_src_res_m,
                    dominant_src_res_m_t2=scene_t2_metadata.dominant_src_res_m,
                ),
                preview_images=PreviewImages(),
                change_polygons_geojson=change_polygons_geojson,
                tabular_metrics=tabular_metrics,
                artifacts=artifacts,
                diagnostics=DiagnosticMetadata(
                    cache_hit=False,
                    stage_seconds=timings.values,
                    tile_counts={
                        "t1": scene_t1.tile_count,
                        "t2": scene_t2.tile_count,
                        "total": scene_t1.tile_count + scene_t2.tile_count,
                    },
                    patch_count=tiled_result.processed_tiles,
                    thresholds={
                        "change_threshold": change_threshold,
                        "semantic_threshold": semantic_threshold,
                        "old_building_mask_dilation_pixels": float(old_building_mask_dilation_pixels),
                        "new_building_core_distance_pixels": float(new_building_core_distance_pixels),
                    },
                    min_new_building_pixels=min_pixels,
                    alignment={"method": "prealigned_windowed_read", "coregistration_skipped": True},
                    backend=backend_diagnostics,
                    warnings=run_warnings,
                    coverage={
                        "t1": _coverage_entry(prepared.t1_release.identifier, resolved_t1.zoom, scene_t1_metadata, tilemap_t1),
                        "t2": _coverage_entry(
                            MAPBOX_SOURCE_ID if use_mapbox_t2 else prepared.t2_release.identifier,
                            resolved_t2.zoom,
                            scene_t2_metadata,
                            tilemap_t2,
                        ),
                    },
                ),
            )
            save_cached_response(settings, prepared.request_hash, response)
            _write_manifest_with_timing(
                timing=timing,
                result_dir=result_dir,
                artifacts=artifacts,
                extra_artifacts=_source_manifest_entries_for_scenes(
                    request_dir=result_dir,
                    run_id=prepared.request_hash,
                    scenes=[scene_t1, scene_t2],
                ),
                run_metadata={**run_identity, "inference_mode": "tiled", "tiled_inference": tiled_result.metadata, "imagery_sources": imagery_source_diagnostics},
            )
            try:
                response.downloadable_zip_path = str(create_export_bundle_from_manifest(result_dir))
                save_cached_response(settings, prepared.request_hash, response)
            except Exception as exc:  # Export bundle is optional for the heavy tiled path.
                LOGGER.warning("TILED_INFERENCE_EXPORT_BUNDLE_SKIPPED runId=%s reason=%s", prepared.request_hash, exc)
            _report(progress, 1.0, "Completed")
            run_succeeded = True
            return response

        _report(progress, 0.35, "Aligning mosaics")
        try:
            with timings.track("mosaic", method="reprojection_only", arosics_enabled=False):
                alignment_result = align_mosaic_pair(
                    scene_t1,
                    scene_t2,
                    settings=settings,
                    out_dir=run_tmp_dir,
                )
        except RuntimeError as exc:
            return RunResponse(
                success=False,
                error_code="co_registration_failed",
                error_message=str(exc),
                diagnostics=_build_failure_diagnostics(
                    timings=timings,
                    prepared=prepared,
                    request=request,
                    min_new_building_pixels=min_pixels,
                    old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                    new_building_core_distance_pixels=new_building_core_distance_pixels,
                    scene_t1_metadata=scene_t1_metadata,
                    scene_t2_metadata=scene_t2_metadata,
                    tilemap_t1=tilemap_t1,
                    tilemap_t2=tilemap_t2,
                    zoom_t1=resolved_t1.zoom,
                    zoom_t2=resolved_t2.zoom,
                    warnings=run_warnings,
                ),
            )

        arr_t1 = alignment_result.t1_rgb
        arr_t2 = alignment_result.t2_rgb
        t1_valid_mask = alignment_result.t1_valid_mask
        t2_valid_mask = alignment_result.t2_valid_mask
        arr_t1, arr_t2, t1_valid_mask, t2_valid_mask, aligned_aoi_mask = _apply_aoi_mask_to_aligned_inputs(
            arr_t1=arr_t1,
            arr_t2=arr_t2,
            t1_valid_mask=t1_valid_mask,
            t2_valid_mask=t2_valid_mask,
            reference_raster_path=scene_t2.geotiff_path,
            normalized_aoi=prepared.normalized_aoi,
        )
        alignment_warnings = [
            str(item)
            for item in alignment_result.diagnostics.get("warnings", [])
            if isinstance(item, str)
        ]
        valid_comparison_mask = t1_valid_mask & t2_valid_mask
        actual_patch_count = estimate_patch_count(
            arr_t1.shape[0],
            arr_t1.shape[1],
            settings.patch_size,
            settings.stride,
        )
        if actual_patch_count > prepared.mode_limits.max_inference_patches_per_scene:
            run_warnings.append(
                f"Aligned imagery requires {actual_patch_count} inference patches per date, exceeding the "
                f"{prepared.mode_limits.label} guidance of {prepared.mode_limits.max_inference_patches_per_scene}."
            )

        _report(progress, 0.45, _inference_stage_message())

        vector_context = VectorizationContext(
            release_t1=prepared.t1_release.identifier,
            release_t2=MAPBOX_SOURCE_ID if use_mapbox_t2 else prepared.t2_release.identifier,
            src_date_t1=scene_t1_metadata.dominant_src_date,
            src_date_t2=scene_t2_metadata.dominant_src_date,
        )

        if model_backend == "bandon_mps":
            try:
                with timings.track("bandon_inference"):
                    bandon_input_prepare_start = time.perf_counter_ns()
                    bandon_input_t1_image = Image.fromarray(arr_t1)
                    bandon_input_t2_image = Image.fromarray(arr_t2)
                    _safe_add_timing_stage(
                        timing,
                        "inference.bandon.input_prepare",
                        duration_ms=_elapsed_ms(bandon_input_prepare_start),
                        metadata={
                            "runner_family": "bandon_mps",
                            "effective_backend": settings.inference_backend,
                            "input_height": int(arr_t1.shape[0]),
                            "input_width": int(arr_t1.shape[1]),
                        },
                    )
                    bandon_input_write_start = time.perf_counter_ns()
                    bandon_input_t1_path, bandon_input_t2_path = _write_bandon_input_images(
                        output_dir=run_tmp_dir,
                        t1_image=bandon_input_t1_image,
                        t2_image=bandon_input_t2_image,
                    )
                    if arr_t1.shape[:2] != t1_valid_mask.shape:
                        raise ValueError(
                            f"T1 valid mask shape {t1_valid_mask.shape} does not match BANDON input shape {arr_t1.shape[:2]}."
                        )
                    if arr_t2.shape[:2] != t2_valid_mask.shape:
                        raise ValueError(
                            f"T2 valid mask shape {t2_valid_mask.shape} does not match BANDON input shape {arr_t2.shape[:2]}."
                        )
                    bandon_t1_valid_mask_path: Path | None = None
                    bandon_t2_valid_mask_path: Path | None = None
                    bandon_aoi_mask_path: Path | None = None
                    bandon_mask_write_metadata: dict[str, object] = {"enabled": False}
                    if settings.bandon_skip_invalid_crops:
                        try:
                            bandon_t1_valid_mask_path = _write_bandon_mask_png(
                                run_tmp_dir,
                                "bandon_t1_valid_mask.png",
                                t1_valid_mask,
                            )
                            bandon_t2_valid_mask_path = _write_bandon_mask_png(
                                run_tmp_dir,
                                "bandon_t2_valid_mask.png",
                                t2_valid_mask,
                            )
                            bandon_aoi_mask = aligned_aoi_mask
                            if bandon_aoi_mask.shape != t2_valid_mask.shape:
                                raise ValueError(
                                    f"AOI mask shape {bandon_aoi_mask.shape} does not match BANDON input shape {t2_valid_mask.shape}."
                                )
                            bandon_aoi_mask_path = _write_bandon_mask_png(
                                run_tmp_dir,
                                "bandon_aoi_mask.png",
                                bandon_aoi_mask,
                            )
                            LOGGER.info(
                                "BANDON_MASK_PATHS_WRITTEN t1Valid=%s t2Valid=%s aoi=%s width=%s height=%s",
                                bandon_t1_valid_mask_path,
                                bandon_t2_valid_mask_path,
                                bandon_aoi_mask_path,
                                int(arr_t2.shape[1]),
                                int(arr_t2.shape[0]),
                            )
                            bandon_mask_write_metadata = {
                                "enabled": True,
                                "t1_valid_mask_path": bandon_t1_valid_mask_path.name,
                                "t2_valid_mask_path": bandon_t2_valid_mask_path.name,
                                "aoi_mask_path": bandon_aoi_mask_path.name,
                                "mask_height": int(t2_valid_mask.shape[0]),
                                "mask_width": int(t2_valid_mask.shape[1]),
                            }
                        except Exception as exc:  # noqa: BLE001
                            bandon_t1_valid_mask_path = None
                            bandon_t2_valid_mask_path = None
                            bandon_aoi_mask_path = None
                            bandon_mask_write_metadata = {
                                "enabled": False,
                                "reason": f"{type(exc).__name__}: {exc}",
                            }
                            LOGGER.warning(
                                "BANDON_MASK_PATHS_WRITTEN enabled=false reason=%s",
                                bandon_mask_write_metadata["reason"],
                            )
                    _safe_add_timing_stage(
                        timing,
                        "inference.bandon.input_write",
                        duration_ms=_elapsed_ms(bandon_input_write_start),
                        metadata={
                            "runner_family": "bandon_mps",
                            "effective_backend": settings.inference_backend,
                            "t1_path": bandon_input_t1_path.name,
                            "t2_path": bandon_input_t2_path.name,
                            "t1_input_path_exists": bandon_input_t1_path.exists(),
                            "t2_input_path_exists": bandon_input_t2_path.exists(),
                            "crop_skip_masks": bandon_mask_write_metadata,
                        },
                    )
                    bandon_result = run_bandon_inference(
                        image_a_path=bandon_input_t1_path,
                        image_b_path=bandon_input_t2_path,
                        settings=settings,
                        out_dir=run_tmp_dir / "bandon_run",
                        t1_valid_mask_path=bandon_t1_valid_mask_path,
                        t2_valid_mask_path=bandon_t2_valid_mask_path,
                        aoi_mask_path=bandon_aoi_mask_path,
                        effective_backend=settings.inference_backend,
                        threshold=change_threshold,
                    )
                    if bandon_result.child_timing is not None:
                        timing.merge_child_timings(bandon_result.child_timing, prefix="inference.bandon")
            except RuntimeError as exc:
                return RunResponse(
                    success=False,
                    error_code="bandon_inference_failed",
                    error_message=str(exc),
                    diagnostics=_build_failure_diagnostics(
                        timings=timings,
                        prepared=prepared,
                        request=request,
                        min_new_building_pixels=min_pixels,
                        old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                        new_building_core_distance_pixels=new_building_core_distance_pixels,
                        scene_t1_metadata=scene_t1_metadata,
                        scene_t2_metadata=scene_t2_metadata,
                        tilemap_t1=tilemap_t1,
                        tilemap_t2=tilemap_t2,
                        zoom_t1=resolved_t1.zoom,
                        zoom_t2=resolved_t2.zoom,
                        warnings=run_warnings + alignment_warnings,
                        backend=backend_diagnostics,
                    ),
                )

            if bandon_result.change_probability.shape != arr_t2.shape[:2]:
                return RunResponse(
                    success=False,
                    error_code="bandon_output_shape_mismatch",
                    error_message=(
                        "BANDON output shape "
                        f"{bandon_result.change_probability.shape} does not match the aligned scene shape {arr_t2.shape[:2]}."
                    ),
                    diagnostics=_build_failure_diagnostics(
                        timings=timings,
                        prepared=prepared,
                        request=request,
                        min_new_building_pixels=min_pixels,
                        old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
                        new_building_core_distance_pixels=new_building_core_distance_pixels,
                        scene_t1_metadata=scene_t1_metadata,
                        scene_t2_metadata=scene_t2_metadata,
                        tilemap_t1=tilemap_t1,
                        tilemap_t2=tilemap_t2,
                        zoom_t1=resolved_t1.zoom,
                        zoom_t2=resolved_t2.zoom,
                        warnings=run_warnings + alignment_warnings,
                        backend=backend_diagnostics,
                    ),
                )

            backend_diagnostics["bandon"] = {
                "effective_backend": bandon_result.metadata.get("effective_backend"),
                "runner_family": bandon_result.metadata.get("runner_family"),
                "launcher": bandon_result.launcher,
                "command": bandon_result.command,
                "checkpoint_path": bandon_result.metadata.get("checkpoint_path"),
                "checkpoint_sha256": bandon_result.metadata.get("checkpoint_sha256"),
                "checkpoint_diagnostics": bandon_result.metadata.get("checkpoint_diagnostics"),
                "threshold": bandon_result.metadata.get("threshold"),
                "config_path": bandon_result.metadata.get("config_path"),
                "normalization_used": bandon_result.metadata.get("normalization_used"),
                "input_order": bandon_result.metadata.get("input_order"),
                "decode_method": bandon_result.metadata.get("decode_method"),
                "probability_stats": bandon_result.metadata.get("probability_stats"),
                "probability_stats_inside_aoi": bandon_result.metadata.get("probability_stats_inside_aoi"),
                "probability_stats_valid_comparison": _probability_stats(
                    bandon_result.change_probability,
                    valid_comparison_mask,
                ),
                "output_min_by_channel": bandon_result.metadata.get("output_min_by_channel"),
                "output_max_by_channel": bandon_result.metadata.get("output_max_by_channel"),
                "output_mean_by_channel": bandon_result.metadata.get("output_mean_by_channel"),
                "output_std_by_channel": bandon_result.metadata.get("output_std_by_channel"),
                "input_t1": bandon_result.metadata.get("input_t1"),
                "input_t2": bandon_result.metadata.get("input_t2"),
                "device": bandon_result.metadata.get("device"),
                "device_resolved": bandon_result.metadata.get("device_resolved"),
                "allow_mps_fallback": bandon_result.metadata.get("allow_mps_fallback"),
                "pytorch_enable_mps_fallback": bandon_result.metadata.get("pytorch_enable_mps_fallback"),
                "mps_built": bandon_result.metadata.get("mps_built"),
                "mps_available": bandon_result.metadata.get("mps_available"),
                "mps_test_cfg": bandon_result.metadata.get("mps_test_cfg"),
            }

            _report(progress, 0.72, "Applying post-processing")
            with timings.track("postprocessing"):
                raw_change_mask = (
                    bandon_result.change_probability >= change_threshold
                ) & bandon_result.change_mask & valid_comparison_mask
                filtered_change_mask = suppress_edge_hugging_components(
                    raw_change_mask,
                    reference_mask=~valid_comparison_mask,
                    min_core_distance_pixels=new_building_core_distance_pixels,
                )
                filtered_change_mask, change_labels = remove_small_components(
                    filtered_change_mask,
                    min_pixels,
                )

            _report(progress, 0.82, "Vectorizing results")
            with timings.track("vectorization"):
                raw_change_df, raw_change_geojson = vectorize_change_regions(
                    filtered_change_mask,
                    scene_t2.geotiff_path,
                    vector_context,
                    probability=bandon_result.change_probability,
                )
                if raw_change_geojson.get("features"):
                    addition_filter_result = filter_addition_candidates(
                        raw_change_geojson,
                        existing_footprint_geojson=request.existing_footprint_geojson,
                        settings=AdditionCandidateFilterSettings(
                            min_area_m2=settings.addition_min_area_m2,
                            max_existing_overlap_ratio=settings.addition_max_existing_overlap_ratio,
                            thin_artifact_max_area_m2=settings.addition_thin_artifact_max_area_m2,
                            thinness_min_ratio=settings.addition_thinness_min_ratio,
                            edge_buffer_m=settings.addition_edge_buffer_m,
                            max_edge_overlap_ratio=settings.addition_max_edge_overlap_ratio,
                            thin_artifact_max_mean_probability=settings.addition_thin_artifact_max_mean_probability,
                        ),
                    )
                    filtered_change_mask = _rasterize_wgs84_feature_collection_like(
                        addition_filter_result.kept_geojson,
                        scene_t2.geotiff_path,
                    )
                    filtered_change_mask, change_labels = remove_small_components(filtered_change_mask, 1)
                    change_polygons_df = pd.DataFrame(
                        [feature.get("properties", {}) for feature in addition_filter_result.kept_geojson.get("features", [])]
                    )
                    if change_polygons_df.empty:
                        change_polygons_df = pd.DataFrame(columns=list(raw_change_df.columns))
                    change_polygons_geojson = addition_filter_result.kept_geojson
                else:
                    addition_filter_result = None
                    change_polygons_df = raw_change_df
                    change_polygons_geojson = raw_change_geojson
                change_polygons_df, change_polygons_geojson = merge_close_change_regions(
                    change_polygons_geojson,
                    max_gap_m=request.merge_close_gap_m,
                    context=vector_context,
                )
                change_blocks_df, change_blocks_geojson = build_change_blocks(
                    change_polygons_geojson,
                    max_gap_m=request.building_block_gap_m,
                    context=vector_context,
                )
                with timing.stage("buffer_generation", buffer_count=len(request.buffer_distances_m)):
                    change_buffer_layers = build_change_buffer_layers(
                        change_blocks_geojson,
                        distances_m=request.buffer_distances_m,
                        context=vector_context,
                        keep_disjoint_parts_separate=request.keep_disjoint_buffer_parts_separate,
                        road_constraint_layer_path=request.road_constraint_layer_path,
                    )

            _report(progress, 0.92, "Exporting artifacts")
            with timings.track("export"):
                previews, artifacts, zip_path, tabular_metrics = export_bandon_outputs(
                    result_dir=result_dir,
                    reference_raster_path=scene_t2.geotiff_path,
                    t1_rgb=arr_t1,
                    t2_rgb=arr_t2,
                    change_prob=bandon_result.change_probability,
                    change_mask=filtered_change_mask,
                    change_labels=change_labels,
                    change_polygons_df=change_polygons_df,
                    change_polygons_geojson=change_polygons_geojson,
                    change_blocks_df=change_blocks_df,
                    change_blocks_geojson=change_blocks_geojson,
                    buffer_layers=change_buffer_layers,
                    summary_df=pair_summary_df,
                    bandon_metadata_path=run_tmp_dir / "bandon_run" / "run_metadata.json",
                    addition_candidate_diagnostics_geojson=(
                        addition_filter_result.diagnostics_geojson if addition_filter_result is not None else None
                    ),
                    rejected_addition_candidates_geojson=(
                        addition_filter_result.rejected_geojson if addition_filter_result is not None else None
                    ),
                    flagged_addition_candidates_geojson=(
                        addition_filter_result.flagged_geojson if addition_filter_result is not None else None
                    ),
                )

            total_change_area = float(change_polygons_df["area_m2"].sum()) if not change_polygons_df.empty else 0.0
            response = RunResponse(
                success=True,
                summary=SummaryStats(
                    request_hash=prepared.request_hash,
                    mode=request.mode,
                    model_backend=model_backend,
                    result_semantics="building_change",
                    estimated_area_m2=round(prepared.area_m2, 2),
                    tile_count_t1=scene_t1.tile_count,
                    tile_count_t2=scene_t2.tile_count,
                    total_new_buildings=0,
                    total_building_blocks=0,
                    total_new_building_area_m2=0.0,
                    total_building_block_area_m2=0.0,
                    total_change_polygons=int(len(change_polygons_df)),
                    total_change_area_m2=round(total_change_area, 2),
                    release_date_t1=str(prepared.t1_release.release_date),
                    release_date_t2="current_basemap" if use_mapbox_t2 else str(prepared.t2_release.release_date),
                    dominant_src_date_t1=scene_t1_metadata.dominant_src_date,
                    dominant_src_date_t2=scene_t2_metadata.dominant_src_date,
                    dominant_src_res_m_t1=scene_t1_metadata.dominant_src_res_m,
                    dominant_src_res_m_t2=scene_t2_metadata.dominant_src_res_m,
                ),
                preview_images=previews,
                change_polygons_geojson=change_polygons_geojson,
                tabular_metrics=tabular_metrics,
                artifacts=artifacts,
                downloadable_zip_path=zip_path,
                diagnostics=DiagnosticMetadata(
                    cache_hit=False,
                    stage_seconds=timings.values,
                    tile_counts={
                        "t1": scene_t1.tile_count,
                        "t2": scene_t2.tile_count,
                        "total": scene_t1.tile_count + scene_t2.tile_count,
                    },
                    patch_count=0,
                    thresholds={
                        "change_threshold": change_threshold,
                        "semantic_threshold": semantic_threshold,
                        "old_building_mask_dilation_pixels": float(old_building_mask_dilation_pixels),
                        "new_building_core_distance_pixels": float(new_building_core_distance_pixels),
                        "addition_min_area_m2": settings.addition_min_area_m2,
                        "addition_max_existing_overlap_ratio": settings.addition_max_existing_overlap_ratio,
                        "addition_thin_artifact_max_area_m2": settings.addition_thin_artifact_max_area_m2,
                        "addition_thinness_min_ratio": settings.addition_thinness_min_ratio,
                        "addition_edge_buffer_m": settings.addition_edge_buffer_m,
                        "addition_max_edge_overlap_ratio": settings.addition_max_edge_overlap_ratio,
                        "addition_thin_artifact_max_mean_probability": settings.addition_thin_artifact_max_mean_probability,
                    },
                    min_new_building_pixels=min_pixels,
                    alignment=alignment_result.diagnostics,
                    backend=backend_diagnostics,
                    warnings=run_warnings + alignment_warnings,
                    coverage={
                        "t1": _coverage_entry(prepared.t1_release.identifier, resolved_t1.zoom, scene_t1_metadata, tilemap_t1),
                        "t2": _coverage_entry(MAPBOX_SOURCE_ID if use_mapbox_t2 else prepared.t2_release.identifier, resolved_t2.zoom, scene_t2_metadata, tilemap_t2),
                    },
                ),
            )
            save_cached_response(settings, prepared.request_hash, response)
            _write_manifest_with_timing(
                timing=timing,
                result_dir=result_dir,
                artifacts=artifacts,
                extra_artifacts=_source_manifest_entries_for_scenes(
                    request_dir=result_dir,
                    run_id=prepared.request_hash,
                    scenes=[scene_t1, scene_t2],
                ),
                run_metadata={**run_identity, "imagery_sources": imagery_source_diagnostics},
            )
            _report(progress, 1.0, "Completed")
            run_succeeded = True
            return response

    finally:
        if not run_succeeded:
            _write_timing_report_safely(timing, result_dir)
        cleanup_run_tmp_dir(settings, prepared.request_hash, success=run_succeeded)
