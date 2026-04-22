from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
import gradio as gr
import pandas as pd
from PIL import Image
from pathlib import Path
import requests
from typing import Any, Callable, Literal

from src.config import Settings
from src.domain.cache import load_cached_response, request_result_dir, save_cached_response
from src.domain.bandon_runner import run_bandon_inference
from src.domain.exports import export_bandon_outputs, export_run_outputs
from src.domain.inference import derive_new_building_products, run_tiled_inference
from src.domain.mosaic import align_mosaic_pair, download_wayback_mosaic
from src.domain.postprocess import remove_small_components, suppress_edge_hugging_components
from src.domain.tiling import estimate_patch_count
from src.domain.vectorize import (
    VectorizationContext,
    build_building_blocks,
    build_change_blocks,
    build_change_buffer_layers,
    build_metric_buffer_layers,
    merge_close_change_regions,
    merge_close_buildings,
    vectorize_change_regions,
    vectorize_new_buildings,
)
from src.domain.wayback import (
    MetadataSummary,
    TileAvailabilitySummary,
    build_session,
    preflight_wayback_tile_availability,
    summarize_wayback_metadata,
)
from src.schemas import DiagnosticMetadata, RunRequest, RunResponse, SummaryStats
from src.services.releases import list_releases
from src.services.validation import PreparedRequest, resolve_min_new_building_pixels, validate_request
from src.utils.logging import get_logger
from src.utils.profiling import StageTimings


LOGGER = get_logger(__name__)


ProgressReporter = gr.Progress | Callable[[float, str], None] | None


def _report(progress: ProgressReporter, value: float, message: str) -> None:
    if progress is not None:
        if isinstance(progress, gr.Progress):
            progress(value, desc=message)
        else:
            progress(value, message)


def _inference_stage_message(model_backend: Literal["sam3", "bandon_mps"], inference_runner) -> str:
    if model_backend == "bandon_mps":
        return "Running BANDON MTGCDNet change detection"
    runner_name = getattr(inference_runner, "__name__", "")
    if runner_name == "run_tiled_inference":
        return "Running remote SAM3 building extraction"
    if isinstance(inference_runner, partial):
        nested_name = getattr(inference_runner.func, "__name__", "")
        if nested_name == "run_local_tiled_inference":
            return "Running local SAM3 building extraction"
    return "Running building extraction"


def _write_bandon_input_images(
    *,
    result_dir: Path,
    t1_rgb: Any,
    t2_rgb: Any,
) -> tuple[Path, Path]:
    t1_path = result_dir / "bandon_input_t1.png"
    t2_path = result_dir / "bandon_input_t2.png"
    Image.fromarray(t1_rgb).save(t1_path)
    Image.fromarray(t2_rgb).save(t2_path)
    return t1_path, t2_path


def _close_session_if_possible(session: object) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        close()


def _summarize_release_metadata_for_request(
    settings: Settings,
    *,
    release_identifier: str,
    aoi_bbox: dict[str, float],
    normalized_aoi: dict[str, Any],
) -> MetadataSummary:
    session = build_session(settings)
    try:
        return summarize_wayback_metadata(
            session,
            release_identifier,
            aoi_bbox,
            grid_size=settings.metadata_grid_size,
            aoi_geojson=normalized_aoi,
            zoom=settings.zoom,
        )
    finally:
        _close_session_if_possible(session)


def _preflight_release_tile_availability_for_request(
    settings: Settings,
    *,
    release,
    aoi_bbox: dict[str, float],
) -> TileAvailabilitySummary:
    session = build_session(settings)
    try:
        return preflight_wayback_tile_availability(
            session,
            release,
            aoi_bbox,
            zoom=settings.zoom,
            max_workers=settings.wayback_metadata_workers,
        )
    finally:
        _close_session_if_possible(session)


def _pair_summary_df(
    *,
    prepared: PreparedRequest,
    scene_t1_metadata: MetadataSummary,
    scene_t2_metadata: MetadataSummary,
    t1_tilemap: TileAvailabilitySummary | None = None,
    t2_tilemap: TileAvailabilitySummary | None = None,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "label": "t1",
                "identifier": prepared.t1_release.identifier,
                "release_date": str(prepared.t1_release.release_date),
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
                "identifier": prepared.t2_release.identifier,
                "release_date": str(prepared.t2_release.release_date),
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


def _coverage_entry(
    release_identifier: str,
    metadata: MetadataSummary,
    tilemap: TileAvailabilitySummary | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "release_identifier": release_identifier,
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
    warnings: list[str] | None = None,
    backend: dict[str, Any] | None = None,
) -> DiagnosticMetadata:
    coverage: dict[str, Any] = {}
    if scene_t1_metadata is not None:
        coverage["t1"] = _coverage_entry(prepared.t1_release.identifier, scene_t1_metadata, tilemap_t1)
    if scene_t2_metadata is not None:
        coverage["t2"] = _coverage_entry(prepared.t2_release.identifier, scene_t2_metadata, tilemap_t2)
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
    inference_runner=run_tiled_inference,
    model_backend: Literal["sam3", "bandon_mps"] = "sam3",
    remote_patch_budget_enabled: bool = True,
    request_hash_context: dict[str, object] | None = None,
) -> RunResponse:
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

    cached = load_cached_response(settings, prepared.request_hash)
    if cached is not None:
        if cached.diagnostics is not None:
            cached.diagnostics.cache_hit = True
        return cached

    session = build_session(settings)
    timings = StageTimings()
    result_dir = request_result_dir(settings, prepared.request_hash)

    from src.utils.geometry import bounds_dict, parse_aoi_geometry

    geometry = parse_aoi_geometry(prepared.normalized_aoi)
    aoi_bbox = bounds_dict(geometry)
    min_pixels = resolve_min_new_building_pixels(
        request,
        normalized_aoi=prepared.normalized_aoi,
        settings=settings,
    )
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
    run_warnings: list[str] = []
    backend_diagnostics: dict[str, Any] = {"model_backend": model_backend}
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
    _report(progress, 0.05, "Resolving Wayback metadata")

    with timings.track("release_resolution"):
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_t1_metadata = executor.submit(
                _summarize_release_metadata_for_request,
                settings,
                release_identifier=prepared.t1_release.identifier,
                aoi_bbox=aoi_bbox,
                normalized_aoi=prepared.normalized_aoi,
            )
            future_t2_metadata = executor.submit(
                _summarize_release_metadata_for_request,
                settings,
                release_identifier=prepared.t2_release.identifier,
                aoi_bbox=aoi_bbox,
                normalized_aoi=prepared.normalized_aoi,
            )
            scene_t1_metadata = future_t1_metadata.result()
            scene_t2_metadata = future_t2_metadata.result()

    tilemap_t1: TileAvailabilitySummary | None = None
    tilemap_t2: TileAvailabilitySummary | None = None
    if settings.wayback_tilemap_preflight_enabled:
        _report(progress, 0.12, "Checking tile availability")
        with timings.track("availability_preflight"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_t1_tilemap = executor.submit(
                    _preflight_release_tile_availability_for_request,
                    settings,
                    release=prepared.t1_release,
                    aoi_bbox=aoi_bbox,
                )
                future_t2_tilemap = executor.submit(
                    _preflight_release_tile_availability_for_request,
                    settings,
                    release=prepared.t2_release,
                    aoi_bbox=aoi_bbox,
                )
                tilemap_t1 = future_t1_tilemap.result()
                tilemap_t2 = future_t2_tilemap.result()

    for label, release_identifier, metadata, tilemap in (
        ("T1", prepared.t1_release.identifier, scene_t1_metadata, tilemap_t1),
        ("T2", prepared.t2_release.identifier, scene_t2_metadata, tilemap_t2),
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
                run_warnings.append(
                    f"{label} release {release_identifier} has partial z={settings.zoom} tile coverage for the AOI "
                    f"({tilemap.available_count}/{tilemap.candidate_count} tiles available)."
                )

    with timings.track("tile_indexing"):
        pair_summary_df = _pair_summary_df(
            prepared=prepared,
            scene_t1_metadata=scene_t1_metadata,
            scene_t2_metadata=scene_t2_metadata,
            t1_tilemap=tilemap_t1,
            t2_tilemap=tilemap_t2,
        )

    for tilemap, release_identifier, metadata in (
        (tilemap_t1, prepared.t1_release.identifier, scene_t1_metadata),
        (tilemap_t2, prepared.t2_release.identifier, scene_t2_metadata),
    ):
        if tilemap is not None and tilemap.preflight_complete and tilemap.available_count == 0:
            return RunResponse(
                success=False,
                error_code="wayback_tile_coverage_unavailable",
                error_message=_tilemap_unavailability_message(
                    release_identifier=release_identifier,
                    metadata=metadata,
                    tilemap=tilemap,
                    zoom=settings.zoom,
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
                    warnings=run_warnings,
                    backend=backend_diagnostics,
                ),
            )

    _report(progress, 0.18, "Downloading Wayback imagery")
    try:
        with timings.track("download"):
            scene_t1 = download_wayback_mosaic(
                prepared.t1_release,
                aoi_bbox,
                settings=settings,
                out_dir=result_dir,
                label="t1",
                max_tiles=None,
                available_tiles=tilemap_t1.available_tiles if tilemap_t1 is not None and tilemap_t1.preflight_complete else None,
            )
            scene_t2 = download_wayback_mosaic(
                prepared.t2_release,
                aoi_bbox,
                settings=settings,
                out_dir=result_dir,
                label="t2",
                max_tiles=None,
                available_tiles=tilemap_t2.available_tiles if tilemap_t2 is not None and tilemap_t2.preflight_complete else None,
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
                warnings=run_warnings,
                backend=backend_diagnostics,
            ),
        )

    _report(progress, 0.35, "Aligning mosaics")
    try:
        with timings.track("mosaic"):
            alignment_result = align_mosaic_pair(
                scene_t1,
                scene_t2,
                settings=settings,
                out_dir=result_dir,
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
                warnings=run_warnings,
            ),
        )
    arr_t1 = alignment_result.t1_rgb
    arr_t2 = alignment_result.t2_rgb
    t1_valid_mask = alignment_result.t1_valid_mask
    t2_valid_mask = alignment_result.t2_valid_mask
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
    if remote_patch_budget_enabled and actual_patch_count > prepared.mode_limits.max_remote_patches_per_scene:
        return RunResponse(
            success=False,
            error_code="remote_patch_budget_exceeded",
            error_message=(
                "Aligned imagery requires "
                f"{actual_patch_count} remote SAM3 patches per date, exceeding the {prepared.mode_limits.label} "
                f"limit of {prepared.mode_limits.max_remote_patches_per_scene}. Reduce the AOI extent."
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
                warnings=run_warnings + alignment_warnings,
                backend=backend_diagnostics,
            ),
        )

    _report(progress, 0.45, _inference_stage_message(model_backend, inference_runner))

    vector_context = VectorizationContext(
        release_t1=prepared.t1_release.identifier,
        release_t2=prepared.t2_release.identifier,
        src_date_t1=scene_t1_metadata.dominant_src_date,
        src_date_t2=scene_t2_metadata.dominant_src_date,
    )

    if model_backend == "bandon_mps":
        try:
            with timings.track("bandon_inference"):
                bandon_input_t1_path, bandon_input_t2_path = _write_bandon_input_images(
                    result_dir=result_dir,
                    t1_rgb=arr_t1,
                    t2_rgb=arr_t2,
                )
                bandon_result = run_bandon_inference(
                    image_a_path=bandon_input_t1_path,
                    image_b_path=bandon_input_t2_path,
                    settings=settings,
                    out_dir=result_dir / "bandon_run",
                )
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
                    warnings=run_warnings + alignment_warnings,
                    backend=backend_diagnostics,
                ),
            )

        backend_diagnostics["bandon"] = {
            "launcher": bandon_result.launcher,
            "command": bandon_result.command,
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
                bandon_result.change_probability >= float(request.change_threshold)
            ) & bandon_result.change_mask & valid_comparison_mask
            filtered_change_mask = suppress_edge_hugging_components(
                raw_change_mask,
                # The helper measures distance away from the reference mask.
                # For BANDON we want to suppress components hugging the invalid
                # scene boundary, so the reference must be the invalid area.
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
            )
            change_polygons_df, change_polygons_geojson = merge_close_change_regions(
                raw_change_geojson,
                max_gap_m=request.merge_close_gap_m,
                context=vector_context,
            )
            change_blocks_df, change_blocks_geojson = build_change_blocks(
                change_polygons_geojson,
                max_gap_m=request.building_block_gap_m,
                context=vector_context,
            )
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
                bandon_metadata_path=result_dir / "bandon_run" / "run_metadata.json",
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
                release_date_t2=str(prepared.t2_release.release_date),
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
                    "change_threshold": request.change_threshold,
                    "semantic_threshold": request.semantic_threshold,
                    "old_building_mask_dilation_pixels": float(old_building_mask_dilation_pixels),
                    "new_building_core_distance_pixels": float(new_building_core_distance_pixels),
                },
                min_new_building_pixels=min_pixels,
                alignment=alignment_result.diagnostics,
                backend=backend_diagnostics,
                warnings=run_warnings + alignment_warnings,
                coverage={
                    "t1": _coverage_entry(prepared.t1_release.identifier, scene_t1_metadata, tilemap_t1),
                    "t2": _coverage_entry(prepared.t2_release.identifier, scene_t2_metadata, tilemap_t2),
                },
            ),
        )
        save_cached_response(settings, prepared.request_hash, response)
        _report(progress, 1.0, "Completed")
        return response

    probs: dict[str, object]
    try:
        with timings.track("remote_segmentation"):
            probs, inference_diag = inference_runner(
                arr_t1,
                arr_t2,
                settings=settings,
                semantic_threshold=request.semantic_threshold,
                cache_dir=result_dir,
                x_ip_token=x_ip_token,
                progress_callback=lambda message: _report(progress, 0.55, message),
            )
    except RuntimeError as exc:
        message = str(exc)
        error_code = (
            "remote_provider_quota_exhausted"
            if "exceeded your gpu quota" in message.lower()
            else "remote_provider_unavailable"
        )
        return RunResponse(
            success=False,
            error_code=error_code,
            error_message=message,
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
                warnings=run_warnings + alignment_warnings,
                backend=backend_diagnostics,
            ),
        )
    timings.values["patch_preparation"] = inference_diag.patch_prepare_seconds
    timings.values["remote_inference"] = inference_diag.remote_seconds
    timings.values["mask_decode"] = inference_diag.mask_decode_seconds

    _report(progress, 0.72, "Applying post-processing")
    with timings.track("postprocessing"):
        products = derive_new_building_products(
            probs["change_prediction"],  # type: ignore[index]
            probs["t1_semantic_prediction"],  # type: ignore[index]
            probs["t2_semantic_prediction"],  # type: ignore[index]
            change_threshold=request.change_threshold,
            semantic_threshold=request.semantic_threshold,
            min_new_building_pixels=min_pixels,
            old_building_mask_dilation_pixels=old_building_mask_dilation_pixels,
            new_building_core_distance_pixels=new_building_core_distance_pixels,
            valid_comparison_mask=valid_comparison_mask,
        )

    _report(progress, 0.82, "Vectorizing results")
    with timings.track("vectorization"):
        raw_new_buildings_df, raw_new_buildings_geojson = vectorize_new_buildings(
            products["new_building_mask"],
            scene_t2.geotiff_path,
            vector_context,
        )
        new_buildings_df, new_buildings_geojson = merge_close_buildings(
            raw_new_buildings_geojson,
            max_gap_m=request.merge_close_gap_m,
            context=vector_context,
        )
        building_blocks_df, building_blocks_geojson = build_building_blocks(
            new_buildings_geojson,
            max_gap_m=request.building_block_gap_m,
            context=vector_context,
        )
        buffer_layers = build_metric_buffer_layers(
            building_blocks_geojson,
            distances_m=request.buffer_distances_m,
            context=vector_context,
            keep_disjoint_parts_separate=request.keep_disjoint_buffer_parts_separate,
            road_constraint_layer_path=request.road_constraint_layer_path,
        )

    _report(progress, 0.92, "Exporting artifacts")
    with timings.track("export"):
        previews, artifacts, zip_path, tabular_metrics = export_run_outputs(
            result_dir=result_dir,
            reference_raster_path=scene_t2.geotiff_path,
            t1_rgb=arr_t1,
            t2_rgb=arr_t2,
            change_prob=probs["change_prediction"],  # type: ignore[index]
            t1_building_prob=probs["t1_semantic_prediction"],  # type: ignore[index]
            t2_building_prob=probs["t2_semantic_prediction"],  # type: ignore[index]
            t1_building_mask=products["t1_building_mask"],
            t2_building_mask=products["t2_building_mask"],
            new_building_mask=products["new_building_mask"],
            new_building_labels=products["new_building_labels"],
            new_buildings_df=new_buildings_df,
            new_buildings_geojson=new_buildings_geojson,
            building_blocks_df=building_blocks_df,
            building_blocks_geojson=building_blocks_geojson,
            buffer_layers=buffer_layers,
            summary_df=pair_summary_df,
        )

    total_new_building_area = float(new_buildings_df["area_m2"].sum()) if not new_buildings_df.empty else 0.0
    total_block_area = float(building_blocks_df["area_m2"].sum()) if not building_blocks_df.empty else 0.0
    response = RunResponse(
        success=True,
        summary=SummaryStats(
            request_hash=prepared.request_hash,
            mode=request.mode,
            model_backend=model_backend,
            result_semantics="new_buildings",
            estimated_area_m2=round(prepared.area_m2, 2),
            tile_count_t1=scene_t1.tile_count,
            tile_count_t2=scene_t2.tile_count,
            total_new_buildings=int(len(new_buildings_df)),
            total_building_blocks=int(len(building_blocks_df)),
            total_new_building_area_m2=round(total_new_building_area, 2),
            total_building_block_area_m2=round(total_block_area, 2),
            release_date_t1=str(prepared.t1_release.release_date),
            release_date_t2=str(prepared.t2_release.release_date),
            dominant_src_date_t1=scene_t1_metadata.dominant_src_date,
            dominant_src_date_t2=scene_t2_metadata.dominant_src_date,
            dominant_src_res_m_t1=scene_t1_metadata.dominant_src_res_m,
            dominant_src_res_m_t2=scene_t2_metadata.dominant_src_res_m,
        ),
        preview_images=previews,
        new_buildings_geojson=new_buildings_geojson,
        building_blocks_geojson=building_blocks_geojson,
        buffer_layers_geojson={label: geojson for label, (_, geojson) in buffer_layers.items()},
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
            patch_count=inference_diag.patch_count,
            thresholds={
                "change_threshold": request.change_threshold,
                "semantic_threshold": request.semantic_threshold,
                "old_building_mask_dilation_pixels": float(old_building_mask_dilation_pixels),
                "new_building_core_distance_pixels": float(new_building_core_distance_pixels),
            },
            min_new_building_pixels=min_pixels,
            alignment=alignment_result.diagnostics,
            backend=backend_diagnostics,
            warnings=run_warnings + alignment_warnings,
            coverage={
                "t1": _coverage_entry(prepared.t1_release.identifier, scene_t1_metadata, tilemap_t1),
                "t2": _coverage_entry(prepared.t2_release.identifier, scene_t2_metadata, tilemap_t2),
            },
        ),
    )
    save_cached_response(settings, prepared.request_hash, response)
    _report(progress, 1.0, "Completed")
    return response
