from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import csv
import base64
import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable

import orjson
from osgeo import ogr, osr
import rasterio
from rasterio.warp import transform_bounds
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from src.config import Settings
from src.domain.cache import load_cached_response, request_result_dir, save_cached_response
from src.domain.imagery_providers import EsriWaybackProvider
from src.domain.reference_imagery_cache import (
    append_reference_imagery_materialization,
    materialize_reference_imagery_cog,
    read_reference_imagery_cache_metadata,
    write_reference_imagery_cache_metadata,
)
from src.domain.stage_timing import StageTimingRecorder
from src.domain.tiling import tile_bounds_3857, tile_range_for_bbox
from src.domain.vectorize import (
    VectorizationContext,
    build_change_buffer_layers,
    build_temporal_growth_blocks,
)
from src.execution_profiles import PipelineExecutionConfig, resolve_backend, resolve_configured_inference_execution_config
from src.schemas import (
    RunRequest,
    RunResponse,
    ReferenceLayer,
    ReferenceLayerStyle,
    TemporalArtifactEntry,
    TemporalMilestone,
    TemporalMilestoneMetrics,
    TemporalReferenceImagery,
    TemporalOverrideRequest,
    TemporalPairEstimate,
    TemporalProject,
    TemporalProjectRunResponse,
    TemporalProjectSummary,
    TemporalProjectValidationResponse,
    ValidationRequest,
    change_threshold_was_explicit,
    validate_stored_temporal_project,
)
from src.runtime_paths import temporal_project_dir
from src.services.processing import ResolvedWaybackRelease, _cached_response_has_stale_fallback_imagery, _resolve_release_for_aoi
from src.services.temporal_reference_imagery import TemporalReferenceSource, build_temporal_reference_imagery
from src.services.wayback_mosaic_cleanup import cleanup_finalized_temporal_project_wayback_mosaics
from src.services.releases import list_releases
from src.services.validation import validate_request
from src.utils.geometry import bounds_dict, geodesic_area_m2, normalized_aoi_geojson, parse_aoi_geometry


PairRunner = Callable[[RunRequest], RunResponse]


PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,128}$")
PROJECT_REGISTRY_FILENAME = "temporal_projects_registry.json"
PROJECT_COMPACT_METADATA_FILENAME = "project_compact_metadata.json"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemporalMilestonePlanEntry:
    index: int
    release_identifier: str
    previous_release_identifier: str | None
    expected_request_hash: str | None
    cached_response: RunResponse | None
    reusable: bool
    blocking_errors: list[str]


@dataclass(frozen=True)
class TemporalImageryPrefetchPlan:
    pair_index: int
    request_hash: str
    t1_provider: str
    t2_provider: str
    t1_release_identifier: str
    t2_release_identifier: str
    aoi_geojson: dict[str, Any]
    t2_effective_release_identifier: str


@dataclass(frozen=True)
class TemporalImageryPrefetchResult:
    pair_index: int
    request_hash: str
    t1_provider: str
    t2_provider: str
    status: str
    cache_hit_or_warmed: bool
    duration_ms: float
    metadata: dict[str, Any]
    warning: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _empty_feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def _feature_count_from_geojson(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    features = payload.get("features")
    if isinstance(features, list):
        return len(features)
    return 1 if payload.get("type") == "Feature" else 0


def _append_milestone_warning_once(milestone: TemporalMilestone, message: str) -> None:
    if message not in milestone.warnings:
        milestone.warnings.append(message)


def _safe_project_id(project_id: str) -> str:
    if not PROJECT_ID_PATTERN.match(project_id):
        raise ValueError("project_id must be 3-128 characters and only use letters, numbers, '_' or '-'.")
    return project_id


def _milestone_sort_key(milestone: TemporalMilestone) -> tuple[datetime, str]:
    release_date = milestone.release_date or ""
    try:
        parsed_date = datetime.fromisoformat(release_date.replace("Z", "+00:00"))
    except ValueError:
        parsed_date = datetime.max.replace(tzinfo=UTC)
    if parsed_date.tzinfo is None:
        parsed_date = parsed_date.replace(tzinfo=UTC)
    return parsed_date, milestone.release_identifier


def _sort_temporal_milestones(project: TemporalProject) -> TemporalProject:
    project.milestones.sort(key=_milestone_sort_key)
    return project


def _populate_milestone_release_dates(project: TemporalProject, settings: Settings) -> TemporalProject:
    if project.milestones and all(milestone.release_date for milestone in project.milestones):
        return project

    releases_by_id = {release.identifier: release for release in list_releases(settings)}
    for milestone in project.milestones:
        if milestone.release_date:
            continue
        release = releases_by_id.get(milestone.release_identifier)
        if release is not None:
            milestone.release_date = str(release.release_date)
    return project


def _default_temporal_execution_config(settings: Settings) -> PipelineExecutionConfig:
    return resolve_configured_inference_execution_config(settings)


def resolve_temporal_project_execution_config(project: TemporalProject, settings: Settings) -> PipelineExecutionConfig:
    if project.execution_config is not None:
        return project.execution_config

    saw_legacy_pair = False
    for milestone in project.milestones:
        if milestone.status != "complete" or not milestone.pair_request_hash:
            continue
        response = _load_cached_run_response(settings, milestone.pair_request_hash)
        if response is None or response.summary is None:
            continue
        saw_legacy_pair = True
        if response.summary.model_backend == "bandon_mps":
            return PipelineExecutionConfig(inference_backend=settings.inference_backend)

    if saw_legacy_pair:
        return PipelineExecutionConfig(inference_backend=settings.inference_backend)

    return _default_temporal_execution_config(settings)


def _project_dir(settings: Settings, project_id: str) -> Path:
    safe_id = _safe_project_id(project_id)
    path = settings.temporal_projects_dir / safe_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_registry_path(settings: Settings) -> Path:
    return settings.runtime_cache_dir / PROJECT_REGISTRY_FILENAME


def _reference_layer_count_for_project(project_id: str, settings: Settings, *, project_dir: str | Path | None = None) -> int:
    if project_dir is not None:
        base_dir = Path(project_dir).expanduser().resolve()
    else:
        registry = _load_project_registry(settings)
        registered_project_dir = registry.get(project_id)
        if isinstance(registered_project_dir, str) and registered_project_dir:
            base_dir = Path(registered_project_dir).expanduser().resolve()
        else:
            base_dir = (settings.temporal_projects_dir / project_id).resolve()
    path = base_dir / "reference_layers" / "reference_layers.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(payload) if isinstance(payload, list) else 0


def temporal_project_response_payload(project: TemporalProject, settings: Settings) -> dict[str, Any]:
    project_dir = project.project_dir
    reference_layer_count = _reference_layer_count_for_project(project.project_id, settings, project_dir=project_dir)
    payload = project.model_dump(mode="json")
    payload["has_reference_layers"] = reference_layer_count > 0
    payload["reference_layer_count"] = reference_layer_count
    return payload


def _complete_reference_imagery_metadata_payload(
    *,
    project_id: str,
    payload: dict[str, Any],
    project_dir: Path,
) -> bool:
    milestones = payload.get("milestones")
    if not isinstance(milestones, list):
        return False
    changed = False
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        release_identifier = milestone.get("release_identifier")
        if not isinstance(release_identifier, str) or not release_identifier:
            continue
        reference_payload = milestone.get("reference_imagery")
        source_reference = None
        if isinstance(reference_payload, dict):
            try:
                source_reference = TemporalReferenceImagery.model_validate(reference_payload)
            except Exception:
                source_reference = None
        cog_path_value = source_reference.cog_path if source_reference and source_reference.cog_path else None
        cog_path = Path(cog_path_value).expanduser() if cog_path_value else project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
        if not cog_path.is_file():
            continue
        needs_completion = (
            not isinstance(reference_payload, dict)
            or not reference_payload.get("raster_bounds_wgs84")
            or reference_payload.get("minzoom") is None
            or reference_payload.get("maxzoom") is None
            or not reference_payload.get("tiles_url_template")
            or "%7B" in str(reference_payload.get("tiles_url_template"))
            or "%7D" in str(reference_payload.get("tiles_url_template"))
        )
        if not needs_completion:
            continue
        completed = _reference_imagery_from_cog_path(
            project_id=project_id,
            release_identifier=release_identifier,
            cog_path=cog_path,
            source_reference=source_reference,
        )
        milestone["reference_imagery"] = completed.model_dump(mode="json")
        changed = True
    return changed


def _payload_has_incomplete_reference_imagery(payload: dict[str, Any], project_dir: Path) -> bool:
    milestones = payload.get("milestones")
    if not isinstance(milestones, list):
        return False
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        release_identifier = milestone.get("release_identifier")
        if not isinstance(release_identifier, str) or not release_identifier:
            continue
        reference_payload = milestone.get("reference_imagery")
        target_cog_path = project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
        if not isinstance(reference_payload, dict):
            return True
        cog_path_value = reference_payload.get("cog_path") or str(target_cog_path)
        cog_path = Path(str(cog_path_value)).expanduser()
        if not cog_path.is_file():
            return True
        if not reference_payload.get("tilejson_url") or not reference_payload.get("tiles_url_template"):
            return True
        if not reference_payload.get("canonical_cog_path") or not reference_payload.get("reference_imagery_key"):
            return True
    return False


def load_temporal_project_response_payload(project_id: str, settings: Settings) -> dict[str, Any]:
    started_at = time.perf_counter()
    path = _project_json_path(settings, project_id)
    if not path.exists():
        raise FileNotFoundError(f"Unknown temporal project: {project_id}")
    metadata_started_at = time.perf_counter()
    payload = orjson.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid temporal project payload: {project_id}")
    for field in ("has_reference_layers", "reference_layer_count"):
        payload.pop(field, None)
    payload.setdefault("project_id", project_id)
    payload.setdefault("project_dir", str(path.parent))
    project_dir = Path(str(payload.get("project_dir") or path.parent)).expanduser().resolve()
    artifact_metadata_before = json.dumps(payload.get("milestones"), sort_keys=True, default=str)
    externalized_count, empty_baseline_artifacts_removed = _externalize_temporal_artifact_payloads_in_payload(
        project_id=project_id,
        payload=payload,
        project_dir=project_dir,
    )
    if externalized_count or empty_baseline_artifacts_removed:
        logger.info(
            "TEMPORAL_PROJECT_METADATA_PAYLOAD_EXTERNALIZED projectId=%s source=response_payload externalizedArtifacts=%s emptyBaselineArtifactsRemoved=%s",
            project_id,
            externalized_count,
            empty_baseline_artifacts_removed,
        )
    artifact_metadata_repaired = _repair_temporal_artifact_metadata_payload(
        project_id=project_id,
        payload=payload,
        project_dir=project_dir,
    )
    reference_metadata_repaired = _complete_reference_imagery_metadata_payload(
        project_id=project_id,
        payload=payload,
        project_dir=project_dir,
    )
    reference_materialized_count = 0
    if _payload_has_incomplete_reference_imagery(payload, project_dir):
        try:
            project_for_repair = validate_stored_temporal_project(payload)
            project_for_repair.project_dir = str(project_dir)
            reference_materialized_count = _ensure_temporal_project_reference_imagery_from_canonical_cache(
                project=project_for_repair,
                settings=settings,
                project_dir=project_dir,
            )
            if reference_materialized_count:
                payload = project_for_repair.model_dump(mode="json")
                payload.setdefault("project_id", project_id)
                payload.setdefault("project_dir", str(project_dir))
                logger.info(
                    "TEMPORAL_REFERENCE_LOAD_TIME_REPAIR_DONE projectId=%s repairedCount=%s",
                    project_id,
                    reference_materialized_count,
                )
        except Exception:
            logger.debug("TEMPORAL_REFERENCE_LOAD_TIME_REPAIR_FAILED projectId=%s", project_id, exc_info=True)
    metrics_repaired = _repair_temporal_metrics_payload(
        project_id=project_id,
        payload=payload,
        settings=settings,
        project_dir=project_dir,
    )
    milestones = payload.get("milestones")
    if isinstance(milestones, list):
        milestones.sort(key=lambda item: str(item.get("release_date") or "") if isinstance(item, dict) else "")
    artifact_metadata_changed = artifact_metadata_before != json.dumps(payload.get("milestones"), sort_keys=True, default=str)
    if (
        artifact_metadata_changed
        or artifact_metadata_repaired
        or reference_metadata_repaired
        or reference_materialized_count
        or metrics_repaired
        or externalized_count
        or empty_baseline_artifacts_removed
    ):
        try:
            path.write_text(json.dumps(payload, indent=2))
            manifest_path = path.with_name("project_manifest.json")
            if manifest_path.exists():
                manifest_path.write_text(json.dumps(payload, indent=2))
            _write_project_summary(validate_stored_temporal_project(payload), path)
        except Exception:
            logger.debug("TEMPORAL_OUTPUT_ARTIFACT_METADATA_REPAIR_PERSIST_FAILED projectId=%s", project_id, exc_info=True)
    reference_layer_count = _reference_layer_count_for_project(project_id, settings, project_dir=payload.get("project_dir"))
    payload["has_reference_layers"] = reference_layer_count > 0
    payload["reference_layer_count"] = reference_layer_count
    logger.info(
        "PROJECT_LOAD_TIMING projectId=%s phase=metadata ms=%s",
        project_id,
        round((time.perf_counter() - metadata_started_at) * 1000, 2),
    )
    logger.info("PROJECT_LOAD_TIMING projectId=%s phase=layer_availability ms=0.0", project_id)
    logger.info(
        "PROJECT_LOAD_TIMING projectId=%s phase=total ms=%s",
        project_id,
        round((time.perf_counter() - started_at) * 1000, 2),
    )
    return payload


def _resolve_existing_temporal_project_dir(settings: Settings, project_id: str) -> Path:
    safe_project_id = _safe_project_id(project_id)
    registry = _load_project_registry(settings)
    candidates: list[Path] = []
    registry_dir = registry.get(project_id)
    if registry_dir:
        candidates.append(Path(registry_dir).expanduser())
    candidates.append(settings.temporal_projects_dir / safe_project_id)

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and (
            (resolved / "project_summary.json").is_file()
            or (resolved / "project.json").is_file()
            or (resolved / "milestones").is_dir()
        ):
            return resolved
    raise FileNotFoundError(f"Unknown temporal project: {project_id}")


def _load_compact_project_summary(project_id: str, project_dir: Path) -> TemporalProjectSummary:
    summary_path = project_dir / "project_summary.json"
    if summary_path.is_file():
        try:
            summary = TemporalProjectSummary.model_validate(json.loads(summary_path.read_text()))
            if summary.project_id == project_id:
                return summary
        except Exception as exc:
            logger.warning(
                "COMPACT_PROJECT_DETAIL_SUMMARY_INVALID projectId=%s path=%s error=%s",
                project_id,
                summary_path,
                exc,
            )

    milestone_dirs = [path for path in (project_dir / "milestones").iterdir() if path.is_dir()] if (project_dir / "milestones").is_dir() else []
    try:
        updated_at = datetime.fromtimestamp(project_dir.stat().st_mtime, UTC).isoformat()
    except OSError:
        updated_at = _utc_now_iso()
    return TemporalProjectSummary(
        project_id=project_id,
        name=project_id,
        project_dir=str(project_dir),
        project_kind="temporal",
        display_name=_project_summary_display_name(project_id, project_id, len(milestone_dirs)),
        semantics="expansion_only",
        milestone_count=len(milestone_dirs),
        complete_milestone_count=len(milestone_dirs),
        created_at=updated_at,
        updated_at=updated_at,
        download_bundle_path=None,
    )


def _project_compact_metadata_json_path(project_json_path: Path) -> Path:
    return project_json_path.with_name(PROJECT_COMPACT_METADATA_FILENAME)


def _write_project_compact_metadata(project: TemporalProject, project_json_path: Path) -> None:
    project_json_mtime_ns: int | None = None
    try:
        project_json_mtime_ns = project_json_path.stat().st_mtime_ns
    except OSError:
        pass
    payload = {
        "project_id": project.project_id,
        "project_json_mtime_ns": project_json_mtime_ns,
        "aoi_geojson": project.aoi_geojson,
        "milestones": [
            {
                "release_identifier": milestone.release_identifier,
                "release_date": milestone.release_date,
                "status": milestone.status,
                "source_mode": milestone.source_mode,
                "warnings": milestone.warnings,
                "error_message": milestone.error_message,
                "metrics": milestone.metrics.model_dump(mode="json") if milestone.metrics is not None else None,
            }
            for milestone in project.milestones
        ],
    }
    _project_compact_metadata_json_path(project_json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_project_compact_metadata(project_id: str, project_dir: Path) -> dict[str, Any]:
    metadata_path = project_dir / PROJECT_COMPACT_METADATA_FILENAME
    if not metadata_path.is_file():
        return {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "COMPACT_PROJECT_METADATA_INVALID projectId=%s path=%s error=%s",
            project_id,
            metadata_path,
            exc,
        )
        return {}
    if not isinstance(metadata, dict) or metadata.get("project_id") != project_id:
        return {}
    project_json_path = project_dir / "project.json"
    expected_mtime_ns = metadata.get("project_json_mtime_ns")
    if project_json_path.is_file() and isinstance(expected_mtime_ns, int):
        try:
            if project_json_path.stat().st_mtime_ns != expected_mtime_ns:
                logger.info("COMPACT_PROJECT_METADATA_STALE projectId=%s path=%s", project_id, metadata_path)
                return {}
        except OSError:
            return {}
    return metadata


def _compact_artifact_entry(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    path: Path,
    description: str,
    media_type: str,
) -> dict[str, Any]:
    stat = path.stat()
    size_bytes = stat.st_size
    compute_geojson_metadata = media_type != "application/geo+json" or size_bytes < TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES
    feature_count, bbox = (
        _geojson_feature_count_and_bbox(path)
        if compute_geojson_metadata and media_type == "application/geo+json"
        else (None, None)
    )
    empty_artifact = media_type == "application/geo+json" and _is_empty_qgis_geojson_artifact(
        path=path,
        feature_count=feature_count,
        size_bytes=size_bytes,
    )
    if empty_artifact:
        logger.info(
            "COMPACT_PROJECT_DETAIL_SKIPPED_EMPTY_ARTIFACT projectId=%s releaseIdentifier=%s artifactKey=%s sizeBytes=%s",
            project_id,
            release_identifier,
            artifact_key,
            size_bytes,
        )
    artifact_url = f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}"
    tilejson_url = None
    tiles_url_template = None
    vector_source_layer = None
    if not empty_artifact and media_type == "application/geo+json" and _should_advertise_vector_tiles(feature_count, size_bytes):
        tilejson_url = _temporal_vector_tilejson_route(project_id, release_identifier, artifact_key)
        tiles_url_template = _temporal_vector_tiles_route(project_id, release_identifier, artifact_key)
        vector_source_layer = TEMPORAL_VECTOR_TILE_SOURCE_LAYER

    return {
        "exists": not empty_artifact,
        "empty": empty_artifact,
        "kind": "vector_tilejson" if tilejson_url else ("empty_geojson" if empty_artifact else "geojson_fallback"),
        "name": f"{release_identifier}_{artifact_key}",
        "path": str(path),
        "media_type": media_type,
        "description": description,
        "key": artifact_key,
        "feature_count": feature_count,
        "size_bytes": size_bytes,
        "source_mtime_ns": stat.st_mtime_ns,
        "bbox": bbox,
        "artifact_url": artifact_url if not empty_artifact else None,
        "geojson_url": f"{artifact_url}.geojson" if not empty_artifact and media_type == "application/geo+json" else None,
        "download_url": f"{artifact_url}.geojson" if not empty_artifact and media_type == "application/geo+json" else None,
        "tilejson_url": tilejson_url,
        "tiles_url_template": tiles_url_template,
        "source_layer": vector_source_layer,
        "vector_source_layer": vector_source_layer,
        "geojson_fallback_url": artifact_url if not empty_artifact else None,
    }


def _valid_compact_bounds(bounds: list[float] | tuple[float, ...] | None) -> list[float] | None:
    if not bounds or len(bounds) < 4:
        return None
    west, south, east, north = (float(value) for value in bounds[:4])
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        return None
    if west >= east or south >= north:
        return None
    if west < -180 or east > 180 or south < -90 or north > 90:
        return None
    return [west, south, east, north]


def _compact_bounds_center(bounds: list[float] | None) -> list[float] | None:
    if not bounds:
        return None
    west, south, east, north = bounds
    return [(west + east) / 2, (south + north) / 2]


def _merge_compact_bounds(bounds_items: list[list[float] | None]) -> list[float] | None:
    valid_items = [bounds for bounds in bounds_items if bounds]
    if not valid_items:
        return None
    return [
        min(bounds[0] for bounds in valid_items),
        min(bounds[1] for bounds in valid_items),
        max(bounds[2] for bounds in valid_items),
        max(bounds[3] for bounds in valid_items),
    ]


def _compact_reference_cog_bounds_wgs84(cog_path: Path) -> list[float] | None:
    try:
        with rasterio.open(cog_path) as src:
            if src.crs is not None:
                return _valid_compact_bounds(
                    [
                        float(value)
                        for value in transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
                    ]
                )
            return _valid_compact_bounds([float(src.bounds.left), float(src.bounds.bottom), float(src.bounds.right), float(src.bounds.top)])
    except Exception:
        logger.debug("COMPACT_PROJECT_REFERENCE_BOUNDS_FAILED cogPath=%s", cog_path, exc_info=True)
        return None


def load_temporal_project_compact_payload(project_id: str, settings: Settings) -> dict[str, Any]:
    started_at = time.perf_counter()
    logger.info("COMPACT_PROJECT_DETAIL_STARTED projectId=%s", project_id)
    project_dir = _resolve_existing_temporal_project_dir(settings, project_id)
    summary = _load_compact_project_summary(project_id, project_dir)
    compact_metadata = _load_project_compact_metadata(project_id, project_dir)
    milestone_metadata_by_release = {
        str(item.get("release_identifier")): item
        for item in compact_metadata.get("milestones", [])
        if isinstance(item, dict) and item.get("release_identifier")
    }
    milestones_dir = project_dir / "milestones"
    milestone_dirs = sorted((path for path in milestones_dir.iterdir() if path.is_dir()), key=lambda path: path.name) if milestones_dir.is_dir() else []

    milestones: list[dict[str, Any]] = []
    milestone_bounds_items: list[list[float] | None] = []
    artifact_count = 0
    complete_milestone_count = 0
    for milestone_dir in milestone_dirs:
        release_identifier = milestone_dir.name
        milestone_metadata = milestone_metadata_by_release.get(release_identifier, {})
        reference_cog_path = milestone_dir / "reference_imagery_cog.tif"
        reference_exists = reference_cog_path.is_file()
        reference_bounds = _compact_reference_cog_bounds_wgs84(reference_cog_path) if reference_exists else None
        artifacts: dict[str, dict[str, Any]] = {}
        for artifact_key, (_field_path, filename, description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            artifact_path = milestone_dir / filename
            if not artifact_path.is_file():
                continue
            artifacts[artifact_key] = _compact_artifact_entry(
                project_id=project_id,
                release_identifier=release_identifier,
                artifact_key=artifact_key,
                path=artifact_path,
                description=description,
                media_type=media_type,
            )
        artifact_count += sum(1 for artifact in artifacts.values() if artifact.get("exists"))
        milestone_bounds = _merge_compact_bounds(
            [reference_bounds]
            + [
                _valid_compact_bounds(artifact.get("bbox"))
                for artifact in artifacts.values()
                if artifact.get("exists") and not artifact.get("empty")
            ]
        )
        milestone_bounds_items.append(milestone_bounds)
        milestone_has_outputs = reference_exists or any(artifact.get("exists") for artifact in artifacts.values())
        metadata_status = milestone_metadata.get("status") if isinstance(milestone_metadata.get("status"), str) else None
        milestone_status = metadata_status or ("complete" if milestone_has_outputs else "pending")
        complete_milestone_count += 1 if milestone_status == "complete" else 0
        milestones.append(
            {
                "release_identifier": release_identifier,
                "label": release_identifier,
                "release_date": milestone_metadata.get("release_date"),
                "status": milestone_status,
                "source_mode": milestone_metadata.get("source_mode") or "automated",
                "warnings": milestone_metadata.get("warnings") if isinstance(milestone_metadata.get("warnings"), list) else [],
                "error_message": milestone_metadata.get("error_message"),
                "metrics": milestone_metadata.get("metrics") if isinstance(milestone_metadata.get("metrics"), dict) else None,
                "bounds": milestone_bounds,
                "bbox": milestone_bounds,
                "center": _compact_bounds_center(milestone_bounds),
                "reference_imagery": {
                    "exists": reference_exists,
                    "kind": "raster_tilejson" if reference_exists else None,
                    "storage_strategy": "raster_tiles" if reference_exists else None,
                    "cog_path": str(reference_cog_path) if reference_exists else None,
                    "raster_bounds_wgs84": reference_bounds,
                    "tilejson_url": (
                        f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/tilejson.json"
                        if reference_exists
                        else None
                    ),
                    "tiles_url_template": (
                        f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/tiles/{{z}}/{{x}}/{{y}}.png"
                        if reference_exists
                        else None
                    ),
                    "tile_size": 256 if reference_exists else None,
                },
                "artifacts": artifacts,
            }
        )

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "COMPACT_PROJECT_DETAIL_SERVED projectId=%s milestoneCount=%s artifactCount=%s durationMs=%s",
        project_id,
        len(milestones),
        artifact_count,
        duration_ms,
    )
    project_bounds = _merge_compact_bounds(milestone_bounds_items)
    compact_aoi_geojson = compact_metadata.get("aoi_geojson") if isinstance(compact_metadata.get("aoi_geojson"), dict) else None
    return {
        "id": project_id,
        "project_id": project_id,
        "name": summary.name,
        "status": "complete" if milestones and complete_milestone_count == len(milestones) else "pending",
        "project_dir": str(project_dir),
        "semantics": summary.semantics,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "download_bundle_path": summary.download_bundle_path,
        "aoi_geojson": compact_aoi_geojson or _bbox_to_geojson_polygon(project_bounds),
        "bounds": project_bounds,
        "bbox": project_bounds,
        "center": _compact_bounds_center(project_bounds),
        "milestone_count": len(milestones) or summary.milestone_count,
        "complete_milestone_count": complete_milestone_count if milestones else summary.complete_milestone_count,
        "milestones": milestones,
        "loading_mode": "compact",
    }


def _load_project_registry(settings: Settings) -> dict[str, str]:
    path = _project_registry_path(settings)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    registry: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            registry[key] = value
    return registry


def _save_project_registry(settings: Settings, registry: dict[str, str]) -> None:
    path = _project_registry_path(settings)
    path.write_text(json.dumps(registry, indent=2))


def _normalize_project_dir(project_dir: str | None) -> Path | None:
    if not project_dir:
        return None
    return Path(project_dir).expanduser()


def _resolve_project_dir(settings: Settings, project_id: str, project_dir: str | None = None) -> Path:
    registry = _load_project_registry(settings)
    configured_dir = project_dir or registry.get(project_id)
    path = temporal_project_dir(settings, _safe_project_id(project_id), configured_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_json_path(settings: Settings, project_id: str) -> Path:
    return _resolve_project_dir(settings, project_id) / "project.json"


def _project_summary_json_path(project_json_path: Path) -> Path:
    return project_json_path.with_name("project_summary.json")


def _artifact_path_for_milestone(project_dir: Path, release_identifier: str, name: str) -> Path:
    milestone_dir = project_dir / "milestones" / release_identifier
    milestone_dir.mkdir(parents=True, exist_ok=True)
    return milestone_dir / name


def _iter_polygon_geometries(payload: dict[str, Any] | None) -> list[BaseGeometry]:
    if not payload:
        return []

    geometries: list[BaseGeometry] = []
    payload_type = payload.get("type")
    candidates: list[dict[str, Any]] = []

    if payload_type == "FeatureCollection":
        features = payload.get("features")
        if isinstance(features, list):
            for feature in features:
                if isinstance(feature, dict):
                    geometry = feature.get("geometry")
                    if isinstance(geometry, dict):
                        candidates.append(geometry)
    elif payload_type == "Feature":
        geometry = payload.get("geometry")
        if isinstance(geometry, dict):
            candidates.append(geometry)
    else:
        candidates.append(payload)

    for candidate in candidates:
        try:
            geometry = shape(candidate).buffer(0)
        except Exception:
            continue
        if geometry.is_empty:
            continue
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        geometries.append(geometry)
    return geometries


def _geometry_from_geojson(payload: dict[str, Any] | None) -> BaseGeometry:
    geometries = _iter_polygon_geometries(payload)
    if not geometries:
        return GeometryCollection()
    return unary_union(geometries).buffer(0)


def _feature_collection_from_geometry(geometry: BaseGeometry) -> dict[str, Any]:
    if geometry.is_empty:
        return _empty_feature_collection()

    if isinstance(geometry, Polygon):
        geometries = [geometry]
    elif isinstance(geometry, MultiPolygon):
        geometries = list(geometry.geoms)
    else:
        repaired = geometry.buffer(0)
        if repaired.is_empty:
            return _empty_feature_collection()
        if isinstance(repaired, Polygon):
            geometries = [repaired]
        elif isinstance(repaired, MultiPolygon):
            geometries = list(repaired.geoms)
        else:
            return _empty_feature_collection()

    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": mapping(item)}
            for item in geometries
            if not item.is_empty
        ],
    }


def _normalize_project_geometry(payload: dict[str, Any], aoi_geometry: BaseGeometry | None = None) -> dict[str, Any]:
    geometry = _geometry_from_geojson(payload)
    if geometry.is_empty:
        raise ValueError("Imported geometry does not contain any polygon features.")
    if aoi_geometry is not None:
        geometry = geometry.intersection(aoi_geometry).buffer(0)
        if geometry.is_empty:
            raise ValueError("Imported geometry does not intersect the project AOI.")
    return _feature_collection_from_geometry(geometry)


def _part_count(geometry: BaseGeometry) -> int:
    if geometry.is_empty:
        return 0
    if isinstance(geometry, Polygon):
        return 1
    if isinstance(geometry, MultiPolygon):
        return len(list(geometry.geoms))
    return 0


def _build_metrics(
    additions_geometry: BaseGeometry,
    effective_geometry: BaseGeometry,
    *,
    building_level_available: bool,
    effective_building_blocks_geojson: dict[str, Any] | None = None,
    cumulative_growth_blocks_geojson: dict[str, Any] | None = None,
    cumulative_growth_envelope_geojson: dict[str, Any] | None = None,
) -> TemporalMilestoneMetrics:
    added_block_area_m2 = 0.0
    cumulative_block_area_m2 = 0.0
    growth_envelope_area_m2 = 0.0
    added_block_count = 0
    cumulative_block_count = 0

    if effective_building_blocks_geojson is not None:
        effective_blocks_geometry = _geometry_from_geojson(effective_building_blocks_geojson)
        if not effective_blocks_geometry.is_empty:
            added_block_area_m2 = round(geodesic_area_m2(effective_blocks_geometry), 2)
        added_block_count = len(effective_building_blocks_geojson.get("features", []))

    if cumulative_growth_blocks_geojson is not None:
        cumulative_blocks_geometry = _geometry_from_geojson(cumulative_growth_blocks_geojson)
        if not cumulative_blocks_geometry.is_empty:
            cumulative_block_area_m2 = round(geodesic_area_m2(cumulative_blocks_geometry), 2)
        cumulative_block_count = len(cumulative_growth_blocks_geojson.get("features", []))

    if cumulative_growth_envelope_geojson is not None:
        growth_envelope_geometry = _geometry_from_geojson(cumulative_growth_envelope_geojson)
        if not growth_envelope_geometry.is_empty:
            growth_envelope_area_m2 = round(geodesic_area_m2(growth_envelope_geometry), 2)

    return TemporalMilestoneMetrics(
        added_area_m2=round(geodesic_area_m2(additions_geometry), 2) if not additions_geometry.is_empty else 0.0,
        total_area_m2=round(geodesic_area_m2(effective_geometry), 2) if not effective_geometry.is_empty else 0.0,
        additions_feature_count=_part_count(additions_geometry),
        effective_feature_count=_part_count(effective_geometry),
        building_level_available=building_level_available,
        added_block_count=added_block_count,
        cumulative_block_count=cumulative_block_count,
        added_block_area_m2=added_block_area_m2,
        cumulative_block_area_m2=cumulative_block_area_m2,
        growth_envelope_area_m2=growth_envelope_area_m2,
    )


def _disable_temporal_growth_envelope(project_id: str, release_identifier: str | None, *, reason: str = "product_removed") -> None:
    logger.info(
        "TEMPORAL_GROWTH_ENVELOPE_DISABLED projectId=%s releaseIdentifier=%s reason=%s",
        project_id,
        release_identifier,
        reason,
    )


def _artifact_backed_metrics(response: RunResponse, additions_geojson: dict[str, Any]) -> TemporalMilestoneMetrics:
    summary = response.summary
    feature_count = _feature_count_from_geojson(additions_geojson)
    if summary is None:
        return TemporalMilestoneMetrics(
            additions_feature_count=feature_count,
            effective_feature_count=feature_count,
        )

    if summary.result_semantics == "new_buildings":
        area_m2 = float(summary.total_new_building_area_m2 or summary.estimated_area_m2 or 0.0)
        feature_count = int(summary.total_new_buildings or feature_count)
        block_count = int(summary.total_building_blocks or 0)
        block_area_m2 = float(summary.total_building_block_area_m2 or 0.0)
        building_level_available = True
    else:
        area_m2 = float(summary.total_change_area_m2 or summary.estimated_area_m2 or 0.0)
        feature_count = int(summary.total_change_polygons or feature_count)
        block_count = int(summary.total_building_blocks or 0)
        block_area_m2 = float(summary.total_building_block_area_m2 or 0.0)
        building_level_available = False

    return TemporalMilestoneMetrics(
        added_area_m2=round(area_m2, 2),
        total_area_m2=round(area_m2, 2),
        additions_feature_count=feature_count,
        effective_feature_count=feature_count,
        building_level_available=building_level_available,
        added_block_count=block_count,
        cumulative_block_count=block_count,
        added_block_area_m2=round(block_area_m2, 2),
        cumulative_block_area_m2=round(block_area_m2, 2),
        growth_envelope_area_m2=0.0,
    )


def _write_geojson(path: Path, payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    path.write_text(json.dumps(payload, indent=2))
    return str(path)


TEMPORAL_ALLOWED_ARTIFACT_KEYS = {
    "automated_building_blocks",
    "additions",
    "building_change_buffer_10m",
    "building_change_buffer_15m",
    "building_change_buffer_20m",
    "cumulative_building_change_buffer_10m",
    "cumulative_building_change_buffer_15m",
    "cumulative_building_change_buffer_20m",
}

TEMPORAL_LAYER_ARTIFACTS: dict[str, tuple[str, str, str, str]] = {
    "automated_building_blocks": (
        "automated_building_blocks_geojson",
        "automated_building_blocks.geojson",
        "Automated building-level blocks",
        "application/geo+json",
    ),
    "additions": ("additions_geojson", "additions.geojson", "Effective additions since previous milestone", "application/geo+json"),
    "building_change_buffer_10m": (
        "buffer_layers_geojson.10m",
        "building_change_buffer_10m.geojson",
        "Building-change buffer 10 m",
        "application/geo+json",
    ),
    "building_change_buffer_15m": (
        "buffer_layers_geojson.15m",
        "building_change_buffer_15m.geojson",
        "Building-change buffer 15 m",
        "application/geo+json",
    ),
    "building_change_buffer_20m": (
        "buffer_layers_geojson.20m",
        "building_change_buffer_20m.geojson",
        "Building-change buffer 20 m",
        "application/geo+json",
    ),
}


def _allowed_temporal_artifacts(artifacts: list[TemporalArtifactEntry]) -> list[TemporalArtifactEntry]:
    return [artifact for artifact in artifacts if artifact.key in TEMPORAL_ALLOWED_ARTIFACT_KEYS]

TEMPORAL_VECTOR_TILE_SOURCE_LAYER = "results"
TEMPORAL_VECTOR_TILE_MINZOOM = 0
TEMPORAL_VECTOR_TILE_MAXZOOM = 18
TEMPORAL_VECTOR_TILE_EXTENT = 4096
TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES = 10_000_000
TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_FEATURES = 20_000
TEMPORAL_QGIS_GPKG_CONVERSION_VERSION = "gpkg1"
TEMPORAL_QGIS_GPKG_MEDIA_TYPE = "application/geopackage+sqlite3"
TEMPORAL_QGIS_EMPTY_GEOJSON_THRESHOLD_BYTES = 256


@dataclass
class TemporalVectorTileFeatureIndex:
    feature_count: int
    bbox: list[float] | None
    geometries: tuple[BaseGeometry, ...]
    properties: tuple[dict[str, Any], ...]
    tree: STRtree


def _temporal_vector_tilejson_route(project_id: str, release_identifier: str, artifact_key: str) -> str:
    return (
        f"/api/temporal-projects/{quote(project_id, safe='')}"
        f"/milestones/{quote(release_identifier, safe='')}"
        f"/artifacts/{quote(artifact_key, safe='')}/tilejson.json"
    )


def _temporal_vector_tiles_route(project_id: str, release_identifier: str, artifact_key: str) -> str:
    return (
        f"/api/temporal-projects/{quote(project_id, safe='')}"
        f"/milestones/{quote(release_identifier, safe='')}"
        f"/artifacts/{quote(artifact_key, safe='')}/tiles"
        "/{z}/{x}/{y}.mvt"
    )


def _should_advertise_vector_tiles(feature_count: int | None, size_bytes: int | None) -> bool:
    return (feature_count or 0) >= TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_FEATURES or (
        size_bytes or 0
    ) >= TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES


def _is_large_temporal_payload(payload: dict[str, Any] | None, settings: Settings | None) -> bool:
    if settings is None or payload is None:
        return False
    return _feature_count_from_geojson(payload) > settings.temporal_derived_geometry_max_features


def _response_additions_feature_count(response: RunResponse) -> int:
    additions_geojson = response.new_buildings_geojson or response.change_polygons_geojson or _empty_feature_collection()
    return _feature_count_from_geojson(additions_geojson)


def _milestone_large_result_feature_count(milestone: TemporalMilestone, settings: Settings) -> int:
    counts = [
        _feature_count_from_geojson(milestone.additions_geojson),
        _feature_count_from_geojson(milestone.automated_additions_geojson),
        _feature_count_from_geojson(milestone.cumulative_union_geojson),
    ]
    if milestone.metrics is not None:
        counts.extend(
            [
                milestone.metrics.additions_feature_count,
                milestone.metrics.effective_feature_count,
            ]
        )
    for artifact in milestone.artifacts:
        if artifact.key in TEMPORAL_ALLOWED_ARTIFACT_KEYS:
            counts.append(artifact.feature_count or 0)
    feature_count = max(counts or [0])
    return feature_count if feature_count > settings.temporal_derived_geometry_max_features else 0


def _strip_large_inline_temporal_result_payloads(project: TemporalProject, settings: Settings) -> int:
    stripped_count = 0
    field_names = (
        "automated_additions_geojson",
        "automated_candidate_footprint_geojson",
        "effective_footprint_geojson",
        "cumulative_union_geojson",
        "cumulative_growth_blocks_geojson",
        "cumulative_growth_envelope_geojson",
    )
    for milestone in project.milestones:
        for field_name in field_names:
            payload = getattr(milestone, field_name, None)
            if _is_large_temporal_payload(payload, settings):
                setattr(milestone, field_name, None)
                stripped_count += 1
    if stripped_count:
        logger.info(
            "TEMPORAL_PROJECT_LARGE_INLINE_PAYLOADS_STRIPPED projectId=%s strippedPayloads=%s",
            project.project_id,
            stripped_count,
        )
    return stripped_count


def _artifact_payload_for_milestone(milestone: TemporalMilestone, field_path: str) -> dict[str, Any] | None:
    if field_path.startswith("buffer_layers_geojson."):
        key = field_path.split(".", 1)[1]
        return milestone.buffer_layers_geojson.get(key)
    return getattr(milestone, field_path, None)


def _clear_artifact_payload_for_milestone(milestone: TemporalMilestone, field_path: str) -> None:
    if field_path.startswith("buffer_layers_geojson."):
        key = field_path.split(".", 1)[1]
        milestone.buffer_layers_geojson.pop(key, None)
        return
    setattr(milestone, field_path, None)


def _artifact_payload_for_milestone_payload(milestone: dict[str, Any], field_path: str) -> dict[str, Any] | None:
    if field_path.startswith("buffer_layers_geojson."):
        key = field_path.split(".", 1)[1]
        buffer_layers = milestone.get("buffer_layers_geojson")
        if isinstance(buffer_layers, dict):
            payload = buffer_layers.get(key)
            return payload if isinstance(payload, dict) else None
        return None
    payload = milestone.get(field_path)
    return payload if isinstance(payload, dict) else None


def _clear_artifact_payload_for_milestone_payload(milestone: dict[str, Any], field_path: str) -> None:
    if field_path.startswith("buffer_layers_geojson."):
        key = field_path.split(".", 1)[1]
        buffer_layers = milestone.get("buffer_layers_geojson")
        if isinstance(buffer_layers, dict):
            buffer_layers.pop(key, None)
            if not buffer_layers:
                milestone["buffer_layers_geojson"] = {}
        return
    milestone[field_path] = None


def _externalize_temporal_artifact_payloads_in_payload(
    *,
    project_id: str,
    payload: dict[str, Any],
    project_dir: Path,
) -> tuple[int, int]:
    milestones = payload.get("milestones")
    if not isinstance(milestones, list):
        return 0, 0
    baseline_release_identifier = None
    for milestone in milestones:
        if isinstance(milestone, dict):
            release_identifier = milestone.get("release_identifier")
            if isinstance(release_identifier, str) and release_identifier:
                baseline_release_identifier = release_identifier
                break

    externalized_count = 0
    empty_baseline_artifacts_removed = 0
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        release_identifier = milestone.get("release_identifier")
        if not isinstance(release_identifier, str) or not release_identifier:
            continue
        artifacts = milestone.get("artifacts")
        if not isinstance(artifacts, list):
            artifacts = []
        artifacts_by_key = {
            artifact.get("key"): artifact
            for artifact in artifacts
            if isinstance(artifact, dict) and artifact.get("key") in TEMPORAL_ALLOWED_ARTIFACT_KEYS
        }
        for artifact_key, (field_path, filename, description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            artifact_path = _artifact_path_for_milestone(project_dir, release_identifier, filename)
            artifact_payload = _artifact_payload_for_milestone_payload(milestone, field_path)
            if isinstance(artifact_payload, dict) and artifact_payload.get("type") == "FeatureCollection":
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                _write_geojson(artifact_path, artifact_payload)
                externalized_count += 1
            if artifact_path.is_file():
                size_bytes = artifact_path.stat().st_size
                compute_geojson_metadata = (
                    media_type != "application/geo+json"
                    or size_bytes < TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES
                )
                artifact_entry = _temporal_artifact_entry_payload(
                    project_id=project_id,
                    release_identifier=release_identifier,
                    artifact_key=artifact_key,
                    path=artifact_path,
                    description=description,
                    media_type=media_type,
                    compute_geojson_metadata=compute_geojson_metadata,
                )
                if (
                    release_identifier == baseline_release_identifier
                    and media_type == "application/geo+json"
                    and compute_geojson_metadata
                    and (artifact_entry.get("feature_count") or 0) == 0
                ):
                    if artifacts_by_key.pop(artifact_key, None) is not None:
                        empty_baseline_artifacts_removed += 1
                else:
                    artifacts_by_key[artifact_key] = artifact_entry
                if size_bytes >= TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES:
                    logger.info(
                        "TEMPORAL_LARGE_ARTIFACT_EXTERNALIZED projectId=%s releaseIdentifier=%s artifactKey=%s path=%s sizeBytes=%s tilejsonUrl=%s source=payload_metadata",
                        project_id,
                        release_identifier,
                        artifact_key,
                        artifact_path,
                        size_bytes,
                        artifact_entry.get("tilejson_url"),
                    )
            _clear_artifact_payload_for_milestone_payload(milestone, field_path)
        milestone["artifacts"] = list(artifacts_by_key.values())
    qgis_artifact_count = 0
    releases_with_qgis_artifacts: set[str] = set()
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        release_identifier = str(milestone.get("release_identifier") or "")
        for artifact in milestone.get("artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("qgis_preferred_url"):
                qgis_artifact_count += 1
                if release_identifier:
                    releases_with_qgis_artifacts.add(release_identifier)
    logger.info(
        "QGIS_GPKG_METADATA_SUMMARY project_id=%s releases_with_qgis_artifacts=%s qgis_artifact_count=%s empty_artifact_skipped_count=%s duration_ms=0",
        project_id,
        len(releases_with_qgis_artifacts),
        qgis_artifact_count,
        empty_baseline_artifacts_removed,
    )
    return externalized_count, empty_baseline_artifacts_removed


def _externalize_temporal_artifact_payloads_in_project(
    *,
    project: TemporalProject,
    project_dir: Path,
) -> tuple[int, int]:
    externalized_count = 0
    empty_baseline_artifacts_removed = 0
    baseline_release_identifier = project.milestones[0].release_identifier if project.milestones else None
    for milestone in project.milestones:
        artifacts_by_key = {artifact.key: artifact for artifact in milestone.artifacts if artifact.key in TEMPORAL_ALLOWED_ARTIFACT_KEYS}
        for artifact_key, (field_path, filename, description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, filename)
            artifact_payload = _artifact_payload_for_milestone(milestone, field_path)
            if isinstance(artifact_payload, dict) and artifact_payload.get("type") == "FeatureCollection":
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                _write_geojson(artifact_path, artifact_payload)
                externalized_count += 1
            if artifact_path.is_file():
                size_bytes = artifact_path.stat().st_size
                compute_geojson_metadata = (
                    media_type != "application/geo+json"
                    or size_bytes < TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES
                )
                artifact_entry = _temporal_artifact_entry(
                    project_id=project.project_id,
                    release_identifier=milestone.release_identifier,
                    artifact_key=artifact_key,
                    path=artifact_path,
                    description=description,
                    media_type=media_type,
                    compute_geojson_metadata=compute_geojson_metadata,
                )
                if (
                    milestone.release_identifier == baseline_release_identifier
                    and media_type == "application/geo+json"
                    and compute_geojson_metadata
                    and (artifact_entry.feature_count or 0) == 0
                ):
                    if artifacts_by_key.pop(artifact_key, None) is not None:
                        empty_baseline_artifacts_removed += 1
                else:
                    artifacts_by_key[artifact_key] = artifact_entry
                if size_bytes >= TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES:
                    logger.info(
                        "TEMPORAL_LARGE_ARTIFACT_EXTERNALIZED projectId=%s releaseIdentifier=%s artifactKey=%s path=%s sizeBytes=%s tilejsonUrl=%s source=project_metadata",
                        project.project_id,
                        milestone.release_identifier,
                        artifact_key,
                        artifact_path,
                        size_bytes,
                        artifact_entry.tilejson_url,
                    )
            _clear_artifact_payload_for_milestone(milestone, field_path)
        milestone.artifacts = list(artifacts_by_key.values())
    qgis_artifact_count = 0
    releases_with_qgis_artifacts: set[str] = set()
    for milestone in project.milestones:
        for artifact in milestone.artifacts:
            if artifact.qgis_preferred_url:
                qgis_artifact_count += 1
                releases_with_qgis_artifacts.add(milestone.release_identifier)
    logger.info(
        "QGIS_GPKG_METADATA_SUMMARY project_id=%s releases_with_qgis_artifacts=%s qgis_artifact_count=%s empty_artifact_skipped_count=%s duration_ms=0",
        project.project_id,
        len(releases_with_qgis_artifacts),
        qgis_artifact_count,
        empty_baseline_artifacts_removed,
    )
    return externalized_count, empty_baseline_artifacts_removed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _geojson_feature_count_and_bbox(path: Path) -> tuple[int | None, list[float] | None]:
    if path.is_file():
        stat = path.stat()
        try:
            index = _load_geojson_index_for_vector_tiles(str(path), stat.st_mtime_ns, stat.st_size)
            return index.feature_count, index.bbox
        except Exception:
            logger.debug("TEMPORAL_VECTOR_TILE_METADATA_INDEX_FAILED path=%s", path, exc_info=True)
    payload = _load_geojson_file(path)
    if not isinstance(payload, dict):
        return None, None
    features = payload.get("features")
    if not isinstance(features, list):
        return None, None
    bounds: tuple[float, float, float, float] | None = None
    for feature in features:
        geometry_payload = feature.get("geometry") if isinstance(feature, dict) else None
        if not geometry_payload:
            continue
        try:
            geometry = shape(geometry_payload)
        except Exception:
            continue
        if geometry.is_empty:
            continue
        geom_bounds = geometry.bounds
        if bounds is None:
            bounds = geom_bounds
        else:
            bounds = (
                min(bounds[0], geom_bounds[0]),
                min(bounds[1], geom_bounds[1]),
                max(bounds[2], geom_bounds[2]),
                max(bounds[3], geom_bounds[3]),
            )
    return len(features), list(bounds) if bounds is not None else None


def _temporal_artifact_entry(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    path: Path,
    description: str,
    media_type: str,
    compute_geojson_metadata: bool = True,
) -> TemporalArtifactEntry:
    size_bytes = path.stat().st_size if path.is_file() else None
    feature_count, bbox = (
        _geojson_feature_count_and_bbox(path)
        if compute_geojson_metadata and media_type == "application/geo+json"
        else (None, None)
    )
    tilejson_url = None
    tiles_url_template = None
    vector_source_layer = None
    if media_type == "application/geo+json" and _should_advertise_vector_tiles(feature_count, size_bytes):
        tilejson_url = _temporal_vector_tilejson_route(project_id, release_identifier, artifact_key)
        tiles_url_template = _temporal_vector_tiles_route(project_id, release_identifier, artifact_key)
        vector_source_layer = TEMPORAL_VECTOR_TILE_SOURCE_LAYER
    artifact_url = f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}"
    geojson_url = f"{artifact_url}.geojson" if media_type == "application/geo+json" else None
    empty_qgis_artifact = media_type == "application/geo+json" and _is_empty_qgis_geojson_artifact(
        path=path,
        feature_count=feature_count,
        size_bytes=size_bytes,
    )
    if empty_qgis_artifact:
        logger.info(
            "QGIS_GPKG_METADATA_SKIPPED_EMPTY release_identifier=%s artifact_key=%s reason=empty_artifact source_size_bytes=%s feature_count=%s",
            release_identifier,
            artifact_key,
            size_bytes,
            feature_count,
        )
    gpkg_url = f"{artifact_url}.gpkg" if media_type == "application/geo+json" and not empty_qgis_artifact else None
    source_mtime_ns = path.stat().st_mtime_ns if path.is_file() else None
    qgis_cache_key = (
        f"{source_mtime_ns}-{size_bytes}-{TEMPORAL_QGIS_GPKG_CONVERSION_VERSION}"
        if gpkg_url and source_mtime_ns is not None and size_bytes is not None
        else None
    )
    if gpkg_url:
        logger.debug(
            "QGIS_GPKG_METADATA_EXPOSED project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_url=%s source_size_bytes=%s source_mtime_ns=%s",
            project_id,
            release_identifier,
            artifact_key,
            path,
            gpkg_url,
            size_bytes,
            source_mtime_ns,
        )
    return TemporalArtifactEntry(
        name=f"{release_identifier}_{artifact_key}",
        path=str(path),
        media_type=media_type,
        description=description,
        key=artifact_key,
        feature_count=feature_count,
        size_bytes=size_bytes,
        source_mtime_ns=source_mtime_ns,
        qgis_cache_key=qgis_cache_key,
        bbox=bbox,
        sha256=_sha256_file(path) if path.is_file() else None,
        artifact_url=artifact_url,
        geojson_url=geojson_url,
        download_url=geojson_url or artifact_url,
        gpkg_url=gpkg_url,
        qgis_preferred_url=None if empty_qgis_artifact else (gpkg_url or geojson_url or artifact_url),
        qgis_preferred_format="gpkg" if gpkg_url else None,
        qgis_compatible=media_type == "application/geo+json" and not empty_qgis_artifact,
        tilejson_url=tilejson_url,
        tiles_url_template=tiles_url_template,
        vector_source_layer=vector_source_layer,
    )


def _is_empty_qgis_geojson_artifact(*, path: Path | None = None, feature_count: int | None, size_bytes: int | None) -> bool:
    if feature_count == 0:
        return True
    if feature_count is None and size_bytes is not None and size_bytes <= TEMPORAL_QGIS_EMPTY_GEOJSON_THRESHOLD_BYTES:
        if path is None:
            return True
        payload = _load_geojson_file(path)
        features = payload.get("features") if isinstance(payload, dict) else None
        return isinstance(features, list) and len(features) == 0
    return False


def _temporal_artifact_entry_payload(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    path: Path,
    description: str,
    media_type: str,
    compute_geojson_metadata: bool,
) -> dict[str, Any]:
    return _temporal_artifact_entry(
        project_id=project_id,
        release_identifier=release_identifier,
        artifact_key=artifact_key,
        path=path,
        description=description,
        media_type=media_type,
        compute_geojson_metadata=compute_geojson_metadata,
    ).model_dump(mode="json")


def _repair_temporal_artifact_metadata_payload(
    *,
    project_id: str,
    payload: dict[str, Any],
    project_dir: Path,
) -> bool:
    milestones = payload.get("milestones")
    if not isinstance(milestones, list):
        return False
    changed = False
    baseline_release_identifier = None
    for milestone in milestones:
        if isinstance(milestone, dict):
            baseline_release_identifier = milestone.get("release_identifier")
            break

    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        release_identifier = milestone.get("release_identifier")
        if not isinstance(release_identifier, str) or not release_identifier:
            continue
        artifacts = milestone.get("artifacts")
        if not isinstance(artifacts, list):
            artifacts = []
        artifacts_by_key = {
            artifact.get("key"): artifact
            for artifact in artifacts
            if isinstance(artifact, dict) and artifact.get("key") in TEMPORAL_ALLOWED_ARTIFACT_KEYS
        }
        repaired_count = 0
        for artifact_key, (_field_path, filename, description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            artifact_path = _artifact_path_for_milestone(project_dir, release_identifier, filename)
            if not artifact_path.is_file():
                continue
            size_bytes = artifact_path.stat().st_size
            compute_geojson_metadata = media_type != "application/geo+json" or size_bytes < TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES
            candidate = _temporal_artifact_entry_payload(
                project_id=project_id,
                release_identifier=release_identifier,
                artifact_key=artifact_key,
                path=artifact_path,
                description=description,
                media_type=media_type,
                compute_geojson_metadata=compute_geojson_metadata,
            )
            if (
                release_identifier == baseline_release_identifier
                and media_type == "application/geo+json"
                and compute_geojson_metadata
                and (candidate.get("feature_count") or 0) == 0
            ):
                if artifacts_by_key.pop(artifact_key, None) is not None:
                    changed = True
                continue
            existing = artifacts_by_key.get(artifact_key)
            if existing != candidate:
                artifacts_by_key[artifact_key] = candidate
                repaired_count += 1
                changed = True
        if changed:
            milestone["artifacts"] = list(artifacts_by_key.values())
        if repaired_count:
            logger.info(
                "TEMPORAL_OUTPUT_ARTIFACT_METADATA_REPAIRED projectId=%s releaseIdentifier=%s repairedCount=%s source=milestone_files",
                project_id,
                release_identifier,
                repaired_count,
            )
    return changed


def _png_file_to_data_url(path: str | None) -> str | None:
    if not path:
        return None

    file_path = Path(path)
    if not file_path.is_file():
        return None

    try:
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:image/png;base64,{encoded}"


def _reference_imagery_from_pair_response(
    response: RunResponse | None,
    *,
    project_id: str = "legacy-project",
    project_dir: Path | None = None,
    release_identifier: str = "reference",
    use_t1_preview: bool,
    aoi_geojson: dict[str, Any] | None = None,
    include_data_url: bool = True,
    settings: Settings | None = None,
) -> TemporalReferenceImagery | None:
    if response is None:
        return None

    preview_images = response.preview_images
    image_path = None
    image_png_data_url = None
    raster_bounds_wgs84 = None
    if preview_images is not None:
        image_path = preview_images.t1_preview_path if use_t1_preview else preview_images.t2_preview_path
        raster_bounds_wgs84 = preview_images.raster_bounds_wgs84
        if include_data_url:
            image_png_data_url = (
                preview_images.t1_preview_png_data_url
                if use_t1_preview
                else preview_images.t2_preview_png_data_url
            )
    if include_data_url and image_png_data_url is None:
        image_png_data_url = _png_file_to_data_url(image_path)

    source_raster_path = _reference_source_raster_path_from_pair_response(
        response,
        use_t1_preview=use_t1_preview,
        release_identifier=release_identifier,
        settings=settings,
    )
    valid_mask_path = _reference_valid_mask_path_from_pair_response(
        response,
        use_t1_preview=use_t1_preview,
        release_identifier=release_identifier,
        settings=settings,
    )

    if image_path is None and image_png_data_url is None and source_raster_path is None:
        return None

    if project_dir is None:
        if source_raster_path:
            project_dir = Path(source_raster_path).expanduser().resolve().parent
        elif image_path:
            project_dir = Path(image_path).expanduser().resolve().parent
        else:
            project_dir = Path.cwd()

    return build_temporal_reference_imagery(
        project_id=project_id,
        project_dir=project_dir,
        release_identifier=release_identifier,
        source=TemporalReferenceSource(
            image_path=image_path,
            image_png_data_url=image_png_data_url,
            raster_bounds_wgs84=raster_bounds_wgs84,
            source_raster_path=source_raster_path,
            valid_mask_path=valid_mask_path,
            aoi_geojson=aoi_geojson,
        ),
        settings=settings,
    )


def _reference_source_raster_path_from_pair_response(
    response: RunResponse | None,
    *,
    use_t1_preview: bool,
    release_identifier: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    if response is None:
        return None

    artifact_name = "t1_wayback_rgb_tif" if use_t1_preview else "t2_wayback_rgb_tif"
    for artifact in response.artifacts:
        if artifact.name == artifact_name:
            return artifact.path

    preview_images = response.preview_images
    if preview_images is None:
        return _reference_source_raster_path_from_manifest(
            response=response,
            release_identifier=release_identifier,
            settings=settings,
            want_valid_mask=False,
        )
    preview_path = preview_images.t1_preview_path if use_t1_preview else preview_images.t2_preview_path
    if not preview_path:
        return None
    request_dir = Path(preview_path).expanduser().resolve().parent
    fallback_name = "t1_wayback_rgb.tif" if use_t1_preview else "t2_wayback_rgb.tif"
    fallback_path = request_dir / fallback_name
    if fallback_path.is_file():
        return str(fallback_path)
    return _reference_source_raster_path_from_manifest(
        response=response,
        release_identifier=release_identifier,
        settings=settings,
        want_valid_mask=False,
    )


def _reference_valid_mask_path_from_pair_response(
    response: RunResponse | None,
    *,
    use_t1_preview: bool,
    release_identifier: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    if response is None:
        return None
    if response.preview_images is None:
        return _reference_source_raster_path_from_manifest(
            response=response,
            release_identifier=release_identifier,
            settings=settings,
            want_valid_mask=True,
        )
    preview_path = response.preview_images.t1_preview_path if use_t1_preview else response.preview_images.t2_preview_path
    if not preview_path:
        return _reference_source_raster_path_from_manifest(
            response=response,
            release_identifier=release_identifier,
            settings=settings,
            want_valid_mask=True,
        )
    request_dir = Path(preview_path).expanduser().resolve().parent
    pattern = "t1_*_valid_mask.tif" if use_t1_preview else "t2_*_valid_mask.tif"
    matches = sorted(request_dir.glob(pattern))
    if matches:
        return str(matches[0])
    return _reference_source_raster_path_from_manifest(
        response=response,
        release_identifier=release_identifier,
        settings=settings,
        want_valid_mask=True,
    )


def _request_dir_from_pair_response(response: RunResponse | None, settings: Settings | None) -> Path | None:
    if response is None or response.summary is None or not response.summary.request_hash:
        return None
    if settings is not None:
        return settings.request_cache_dir / response.summary.request_hash
    for artifact in response.artifacts:
        if artifact.path:
            candidate = Path(artifact.path).expanduser()
            if candidate.name:
                return candidate.resolve().parent
    return None


def _reference_source_raster_path_from_manifest(
    *,
    response: RunResponse | None,
    release_identifier: str | None,
    settings: Settings | None,
    want_valid_mask: bool,
) -> str | None:
    if response is None or not release_identifier:
        return None
    request_dir = _request_dir_from_pair_response(response, settings)
    if request_dir is None:
        return None
    manifest_path = request_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug(
            "TEMPORAL_REFERENCE_MANIFEST_READ_FAILED requestDir=%s releaseIdentifier=%s",
            request_dir,
            release_identifier,
            exc_info=True,
        )
        return None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    selected: list[Path] = []
    for item in artifacts:
        if not isinstance(item, dict) or item.get("artifact_type") != "source" or item.get("format") != "tif":
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("provider") != "esri_wayback":
            continue
        source_id = metadata.get("source_id")
        purpose = str(item.get("purpose") or "")
        if source_id != release_identifier and release_identifier not in purpose:
            continue
        is_valid_mask = "valid mask" in purpose.lower() or str(item.get("path") or "").endswith("valid_mask.tif")
        if is_valid_mask != want_valid_mask:
            continue
        path_value = item.get("resolved_path") or item.get("path")
        if not isinstance(path_value, str) or not path_value:
            continue
        candidate = Path(path_value).expanduser().resolve()
        if candidate.is_file():
            selected.append(candidate)
    if len(selected) != 1:
        if selected:
            logger.info(
                "TEMPORAL_REFERENCE_MANIFEST_SOURCE_AMBIGUOUS requestDir=%s releaseIdentifier=%s wantValidMask=%s count=%s",
                request_dir,
                release_identifier,
                want_valid_mask,
                len(selected),
            )
        return None
    logger.info(
        "TEMPORAL_REFERENCE_MANIFEST_SOURCE_RESOLVED requestDir=%s releaseIdentifier=%s wantValidMask=%s path=%s",
        request_dir,
        release_identifier,
        want_valid_mask,
        selected[0],
    )
    return str(selected[0])


def _temporal_reference_aoi_hash(aoi_geojson: dict[str, Any] | None) -> str:
    if not aoi_geojson:
        return "no-aoi"
    normalized = normalized_aoi_geojson(aoi_geojson)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _find_matching_canonical_reference_imagery(
    *,
    settings: Settings,
    release_identifier: str,
    aoi_hash: str,
) -> tuple[Path, dict[str, Any]] | None:
    cache_dir = settings.reference_imagery_cache_dir
    if not settings.reference_imagery_cache_enabled or cache_dir is None or not cache_dir.is_dir():
        return None

    matches: list[tuple[Path, dict[str, Any]]] = []
    for metadata_path in sorted(cache_dir.glob("refimg-v1-*/metadata.json")):
        metadata = read_reference_imagery_cache_metadata(metadata_path)
        if not metadata:
            continue
        metadata_release = metadata.get("release_identifier") or metadata.get("source_release_identifier")
        if metadata_release != release_identifier or metadata.get("aoi_hash") != aoi_hash:
            continue
        canonical_value = metadata.get("canonical_cog_path")
        if not isinstance(canonical_value, str) or not canonical_value:
            continue
        canonical_cog_path = Path(canonical_value).expanduser().resolve()
        if canonical_cog_path.is_file() and canonical_cog_path.stat().st_size > 0:
            matches.append((canonical_cog_path, metadata))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.info(
            "TEMPORAL_REFERENCE_CANONICAL_CACHE_REJECTED releaseIdentifier=%s aoiHash=%s reason=ambiguous_canonical_cache_matches count=%s",
            release_identifier,
            aoi_hash,
            len(matches),
        )
    return None


def _temporal_reference_route(project_id: str, release_identifier: str, suffix: str) -> str:
    return f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/{suffix}"


def _reference_imagery_from_cog_path(
    *,
    project_id: str,
    release_identifier: str,
    cog_path: Path,
    source_reference: TemporalReferenceImagery | None = None,
    reference_imagery_key: str | None = None,
    canonical_cog_path: Path | str | None = None,
    materialization_method: str | None = None,
) -> TemporalReferenceImagery:
    raster_bounds_wgs84 = source_reference.raster_bounds_wgs84 if source_reference else None
    minzoom = source_reference.minzoom if source_reference else None
    maxzoom = source_reference.maxzoom if source_reference else None
    tile_size = source_reference.tile_size if source_reference and source_reference.tile_size else 256
    cog_crs = None
    cog_width = None
    cog_height = None
    try:
        with rasterio.open(cog_path) as src:
            cog_crs = str(src.crs) if src.crs else None
            cog_width = int(src.width)
            cog_height = int(src.height)
            if raster_bounds_wgs84 is None and src.crs is not None:
                raster_bounds_wgs84 = [
                    float(value)
                    for value in transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
                ]
            minzoom = 0 if minzoom is None else minzoom
            maxzoom = 18 if maxzoom is None else maxzoom
    except Exception:
        logger.debug(
            "TEMPORAL_REFERENCE_METADATA_COMPLETION_FAILED projectId=%s releaseIdentifier=%s cogPath=%s",
            project_id,
            release_identifier,
            cog_path,
            exc_info=True,
        )
    logger.info(
        "TEMPORAL_REFERENCE_METADATA_COMPLETED projectId=%s releaseIdentifier=%s bounds=%s minzoom=%s maxzoom=%s tileSize=%s cogPath=%s crs=%s width=%s height=%s",
        project_id,
        release_identifier,
        raster_bounds_wgs84,
        minzoom,
        maxzoom,
        tile_size,
        cog_path,
        cog_crs,
        cog_width,
        cog_height,
    )
    return TemporalReferenceImagery(
        image_path=source_reference.image_path if source_reference else None,
        image_png_data_url=None,
        raster_bounds_wgs84=raster_bounds_wgs84,
        storage_strategy="raster_tiles",
        cog_path=str(cog_path),
        cog_url=f"/api/files?path={quote(str(cog_path))}",
        tilejson_url=_temporal_reference_route(project_id, release_identifier, "tilejson.json"),
        tiles_url_template=_temporal_reference_route(project_id, release_identifier, "tiles/{z}/{x}/{y}.png"),
        minzoom=minzoom,
        maxzoom=maxzoom,
        tile_size=tile_size,
        reference_imagery_key=reference_imagery_key
        or (source_reference.reference_imagery_key if source_reference else None),
        canonical_cog_path=str(canonical_cog_path)
        if canonical_cog_path is not None
        else (source_reference.canonical_cog_path if source_reference else None),
        materialization_method=materialization_method
        or (source_reference.materialization_method if source_reference else None),
    )


def _link_or_copy_reference_cog(source_path: Path, target_path: Path) -> str:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        return "existing"
    try:
        target_path.hardlink_to(source_path)
        return "linked"
    except OSError:
        try:
            target_path.symlink_to(source_path)
            return "symlinked"
        except OSError:
            shutil.copy2(source_path, target_path)
            return "copied"


def _temporal_reference_transform_is_identity(transform: Any) -> bool:
    try:
        return (
            math.isclose(float(transform.a), 1.0)
            and math.isclose(float(transform.b), 0.0)
            and math.isclose(float(transform.c), 0.0)
            and math.isclose(float(transform.d), 0.0)
            and math.isclose(float(transform.e), 1.0)
            and math.isclose(float(transform.f), 0.0)
        )
    except Exception:
        return False


def _reference_raster_is_project_reusable(source_path: Path) -> tuple[bool, str]:
    try:
        with rasterio.open(source_path) as src:
            if src.crs is None:
                return False, "missing_crs"
            if _temporal_reference_transform_is_identity(src.transform):
                return False, "identity_transform"
            if src.width <= 0 or src.height <= 0:
                return False, "empty_dimensions"
            if not src.is_tiled:
                return False, "not_tiled"
            if not src.bounds or src.bounds.left == src.bounds.right or src.bounds.bottom == src.bounds.top:
                return False, "empty_bounds"
    except Exception as exc:  # noqa: BLE001
        return False, f"open_failed:{exc.__class__.__name__}"
    return True, "tiled_local_raster"


def _find_matching_project_reference_cog(
    *,
    settings: Settings,
    project: TemporalProject,
    release_identifier: str,
    aoi_hash: str,
) -> tuple[str, Path, TemporalReferenceImagery | None] | None:
    logger.info(
        "REFERENCE_REUSE_PROJECT_SCAN_START projectId=%s releaseIdentifier=%s aoiHash=%s",
        project.project_id,
        release_identifier,
        aoi_hash,
    )
    if aoi_hash == "no-aoi":
        return None
    for project_json_path in sorted(settings.temporal_projects_dir.glob("*/project.json")):
        source_project_id = project_json_path.parent.name
        if source_project_id == project.project_id:
            continue
        try:
            project_json_size = project_json_path.stat().st_size
        except OSError:
            continue
        if project_json_size > TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES:
            logger.info(
                "REFERENCE_REUSE_PROJECT_SCAN_SKIPPED projectId=%s releaseIdentifier=%s sourceProjectId=%s reason=metadata_too_large bytes=%s",
                project.project_id,
                release_identifier,
                source_project_id,
                project_json_size,
            )
            continue
        try:
            payload = json.loads(project_json_path.read_text())
            candidate = validate_stored_temporal_project(payload)
        except Exception:
            continue
        if _temporal_reference_aoi_hash(candidate.aoi_geojson) != aoi_hash:
            continue
        for milestone in candidate.milestones:
            if milestone.release_identifier != release_identifier:
                continue
            reference = milestone.reference_imagery
            candidate_paths: list[Path] = []
            if reference and reference.cog_path:
                candidate_paths.append(Path(reference.cog_path).expanduser().resolve())
            candidate_paths.append(project_json_path.parent / "milestones" / release_identifier / "reference_imagery_cog.tif")
            for candidate_path in candidate_paths:
                if candidate_path.is_file():
                    logger.info(
                        "REFERENCE_REUSE_PROJECT_MATCH projectId=%s releaseIdentifier=%s aoiHash=%s sourceProjectId=%s sourcePath=%s",
                        project.project_id,
                        release_identifier,
                        aoi_hash,
                        source_project_id,
                        candidate_path,
                    )
                    return source_project_id, candidate_path, reference
    return None


def _project_expected_mosaic_signature(project: TemporalProject, zoom: int) -> tuple[int, tuple[int, int, int, int]] | None:
    if project.aoi_geojson is None:
        return None
    try:
        bbox = bounds_dict(parse_aoi_geometry(project.aoi_geojson))
        tile_range = tile_range_for_bbox(bbox, zoom)
    except Exception:
        return None
    return zoom, tile_range


def _metadata_matches_project_mosaic(
    metadata: dict[str, Any],
    *,
    project: TemporalProject,
    release_identifier: str,
) -> tuple[bool, str]:
    if metadata.get("release_identifier") != release_identifier:
        return False, "release_mismatch"
    try:
        metadata_zoom = int(metadata.get("zoom", -1))
    except (TypeError, ValueError):
        return False, "missing_metadata"
    if metadata_zoom < 0:
        return False, "missing_metadata"
    expected_signature = _project_expected_mosaic_signature(project, metadata_zoom)
    if expected_signature is None:
        return False, "aoi_mismatch"
    expected_zoom, expected_tile_range = expected_signature
    raw_tile_range = metadata.get("tile_range")
    if isinstance(raw_tile_range, list) and len(raw_tile_range) == 4:
        try:
            observed_tile_range = tuple(int(value) for value in raw_tile_range)
        except (TypeError, ValueError):
            return False, "missing_metadata"
        if observed_tile_range == expected_tile_range:
            return True, "same_release_mosaic"
        return False, "tile_range_mismatch"

    raw_bounds = metadata.get("bounds_3857")
    if not (isinstance(raw_bounds, list) and len(raw_bounds) == 4):
        return False, "missing_metadata"
    left, _, _, top = tile_bounds_3857(expected_tile_range[0], expected_tile_range[2], expected_zoom)
    _, bottom, right, _ = tile_bounds_3857(expected_tile_range[1], expected_tile_range[3], expected_zoom)
    expected_bounds = (left, bottom, right, top)
    try:
        if all(abs(float(observed) - expected) <= 1e-6 for observed, expected in zip(raw_bounds, expected_bounds)):
            return True, "same_release_mosaic"
        return False, "aoi_mismatch"
    except (TypeError, ValueError):
        return False, "missing_metadata"


def _find_matching_shared_mosaic(
    *,
    settings: Settings,
    project: TemporalProject,
    release_identifier: str,
    aoi_hash: str,
) -> tuple[Path, Path | None] | None:
    baseline_release_identifier = project.milestones[0].release_identifier if project.milestones else None
    is_baseline_release = release_identifier == baseline_release_identifier
    logger.info(
        "REFERENCE_REUSE_SHARED_CACHE_CHECK projectId=%s releaseIdentifier=%s aoiHash=%s cacheDir=%s",
        project.project_id,
        release_identifier,
        aoi_hash,
        settings.wayback_mosaic_cache_dir,
    )
    if is_baseline_release:
        logger.info(
            "REFERENCE_BASELINE_IMAGERY_RESOLVE_START projectId=%s releaseIdentifier=%s aoiHash=%s cacheDir=%s",
            project.project_id,
            release_identifier,
            aoi_hash,
            settings.wayback_mosaic_cache_dir,
        )
    if aoi_hash == "no-aoi":
        return None
    for metadata_path in sorted(settings.wayback_mosaic_cache_dir.glob("*/metadata.json")):
        try:
            metadata = json.loads(metadata_path.read_text())
        except Exception:
            continue
        if is_baseline_release:
            logger.info(
                "REFERENCE_BASELINE_IMAGERY_SHARED_MOSAIC_CANDIDATE projectId=%s releaseIdentifier=%s aoiHash=%s metadataPath=%s metadataRelease=%s metadataZoom=%s",
                project.project_id,
                release_identifier,
                aoi_hash,
                metadata_path,
                metadata.get("release_identifier"),
                metadata.get("zoom"),
            )
        matches, reason = _metadata_matches_project_mosaic(
            metadata,
            project=project,
            release_identifier=release_identifier,
        )
        if not matches:
            if is_baseline_release and metadata.get("release_identifier") == release_identifier:
                logger.info(
                    "REFERENCE_BASELINE_IMAGERY_SHARED_MOSAIC_REJECTED projectId=%s releaseIdentifier=%s aoiHash=%s metadataPath=%s reason=%s",
                    project.project_id,
                    release_identifier,
                    aoi_hash,
                    metadata_path,
                    reason,
                )
            continue
        mosaic_path = metadata_path.parent / "mosaic.tif"
        if not mosaic_path.is_file():
            if is_baseline_release:
                logger.info(
                    "REFERENCE_BASELINE_IMAGERY_SHARED_MOSAIC_REJECTED projectId=%s releaseIdentifier=%s aoiHash=%s metadataPath=%s reason=missing_mosaic_tif",
                    project.project_id,
                    release_identifier,
                    aoi_hash,
                    metadata_path,
                )
            continue
        valid_mask_path = metadata_path.parent / "valid_mask.tif"
        logger.info(
            "REFERENCE_REUSE_SHARED_CACHE_MATCH projectId=%s releaseIdentifier=%s aoiHash=%s sourcePath=%s reason=%s zoom=%s",
            project.project_id,
            release_identifier,
            aoi_hash,
            mosaic_path,
            reason,
            metadata.get("zoom"),
        )
        if is_baseline_release:
            logger.info(
                "REFERENCE_BASELINE_IMAGERY_SHARED_MOSAIC_MATCH projectId=%s releaseIdentifier=%s aoiHash=%s sourcePath=%s reason=%s zoom=%s",
                project.project_id,
                release_identifier,
                aoi_hash,
                mosaic_path,
                reason,
                metadata.get("zoom"),
            )
        return mosaic_path, valid_mask_path if valid_mask_path.is_file() else None
    if is_baseline_release:
        logger.info(
            "REFERENCE_BASELINE_IMAGERY_UNAVAILABLE projectId=%s releaseIdentifier=%s aoiHash=%s reason=no_project_cog_or_shared_mosaic",
            project.project_id,
            release_identifier,
            aoi_hash,
        )
    return None


def _persist_temporal_project_reference_repair(project: TemporalProject, settings: Settings) -> None:
    project.updated_at = _utc_now_iso()
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    project.project_dir = str(project_dir)
    reference_materialized_count = _ensure_temporal_project_reference_imagery_from_canonical_cache(
        project=project,
        settings=settings,
        project_dir=project_dir,
    )
    if reference_materialized_count:
        logger.info(
            "TEMPORAL_REFERENCE_FINALIZATION_DONE projectId=%s materializedCount=%s",
            project.project_id,
            reference_materialized_count,
        )
    externalized_count, empty_baseline_artifacts_removed = _externalize_temporal_artifact_payloads_in_project(
        project=project,
        project_dir=project_dir,
    )
    _strip_large_inline_temporal_result_payloads(project, settings)
    if externalized_count or empty_baseline_artifacts_removed:
        logger.info(
            "TEMPORAL_PROJECT_METADATA_PAYLOAD_EXTERNALIZED projectId=%s source=reference_repair externalizedArtifacts=%s emptyBaselineArtifactsRemoved=%s",
            project.project_id,
            externalized_count,
            empty_baseline_artifacts_removed,
        )
    payload = project.model_dump(mode="json")
    project_json_path = project_dir / "project.json"
    project_json_path.write_text(json.dumps(payload, indent=2))
    manifest_path = project_dir / "project_manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2))
    logger.info(
        "TEMPORAL_PROJECT_MANIFEST_UPDATED projectId=%s path=%s reason=reference_imagery_repair",
        project.project_id,
        manifest_path,
    )
    _write_project_summary(project, project_json_path)
    logger.info(
        "TEMPORAL_PROJECT_SUMMARY_UPDATED projectId=%s path=%s reason=reference_imagery_repair",
        project.project_id,
        _project_summary_json_path(project_json_path),
    )


def repair_temporal_project_reference_imagery(project_id: str, settings: Settings) -> tuple[TemporalProject, int]:
    started_at = time.perf_counter()
    project = _load_project(
        settings,
        project_id,
        hydrate_reference_imagery=False,
        hydrate_buffer_layers=False,
        refresh_derived_layers=False,
        write_side_effects=False,
    )
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    aoi_hash = _temporal_reference_aoi_hash(project.aoi_geojson)
    repaired_count = 0
    logger.info(
        "TEMPORAL_REFERENCE_REPAIR_START projectId=%s aoiHash=%s milestoneCount=%s",
        project.project_id,
        aoi_hash,
        len(project.milestones),
    )
    logger.info("REFERENCE_REUSE_LOOKUP_START projectId=%s aoiHash=%s", project.project_id, aoi_hash)

    for milestone in project.milestones:
        release_identifier = milestone.release_identifier
        target_cog_path = project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
        logger.info(
            "REFERENCE_LAYERS_REPAIR_SCAN_MILESTONE projectId=%s releaseIdentifier=%s path=%s",
            project.project_id,
            release_identifier,
            target_cog_path,
        )
        if milestone.reference_imagery and milestone.reference_imagery.cog_path and Path(milestone.reference_imagery.cog_path).is_file():
            continue
        if target_cog_path.is_file():
            logger.info(
                "TEMPORAL_REFERENCE_SOURCE_RESOLVED projectId=%s releaseIdentifier=%s source=project_local_cog sourcePath=%s",
                project.project_id,
                release_identifier,
                target_cog_path,
            )
            milestone.reference_imagery = _reference_imagery_from_cog_path(
                project_id=project.project_id,
                release_identifier=release_identifier,
                cog_path=target_cog_path,
                source_reference=milestone.reference_imagery,
            )
            repaired_count += 1
            logger.info(
                "TEMPORAL_REFERENCE_METADATA_REGISTERED projectId=%s releaseIdentifier=%s cogPath=%s tilejsonUrl=%s",
                project.project_id,
                release_identifier,
                target_cog_path,
                milestone.reference_imagery.tilejson_url if milestone.reference_imagery else None,
            )
            logger.info(
                "REFERENCE_REUSE_COG_LINKED projectId=%s releaseIdentifier=%s aoiHash=%s sourcePath=%s targetPath=%s reason=project_local_cog",
                project.project_id,
                release_identifier,
                aoi_hash,
                target_cog_path,
                target_cog_path,
            )
            continue

        canonical_match = _find_matching_canonical_reference_imagery(
            settings=settings,
            release_identifier=release_identifier,
            aoi_hash=aoi_hash,
        )
        if canonical_match is not None:
            canonical_cog_path, canonical_metadata = canonical_match
            logger.info(
                "TEMPORAL_REFERENCE_SOURCE_RESOLVED projectId=%s releaseIdentifier=%s source=canonical_imagery_cache referenceImageryKey=%s canonicalCogPath=%s",
                project.project_id,
                release_identifier,
                canonical_metadata.get("reference_imagery_key"),
                canonical_cog_path,
            )
            materialization = materialize_reference_imagery_cog(
                canonical_cog_path=canonical_cog_path,
                project_cog_path=target_cog_path,
                mode=settings.reference_imagery_materialization,
            )
            append_reference_imagery_materialization(
                canonical_metadata,
                project_id=project.project_id,
                release_identifier=release_identifier,
                project_cog_path=target_cog_path,
                method=str(materialization.get("method") or "unknown"),
            )
            write_reference_imagery_cache_metadata(canonical_cog_path.with_name("metadata.json"), canonical_metadata)
            milestone.reference_imagery = _reference_imagery_from_cog_path(
                project_id=project.project_id,
                release_identifier=release_identifier,
                cog_path=target_cog_path,
                source_reference=milestone.reference_imagery,
                reference_imagery_key=(
                    str(canonical_metadata.get("reference_imagery_key"))
                    if canonical_metadata.get("reference_imagery_key")
                    else None
                ),
                canonical_cog_path=canonical_cog_path,
                materialization_method=str(materialization.get("method") or "unknown"),
            )
            repaired_count += 1
            logger.info(
                "TEMPORAL_REFERENCE_METADATA_REGISTERED projectId=%s releaseIdentifier=%s cogPath=%s canonicalCogPath=%s tilejsonUrl=%s",
                project.project_id,
                release_identifier,
                target_cog_path,
                canonical_cog_path,
                milestone.reference_imagery.tilejson_url if milestone.reference_imagery else None,
            )
            continue

        match = _find_matching_project_reference_cog(
            settings=settings,
            project=project,
            release_identifier=release_identifier,
            aoi_hash=aoi_hash,
        )
        if match is not None:
            source_project_id, source_path, source_reference = match
            logger.info(
                "TEMPORAL_REFERENCE_SOURCE_RESOLVED projectId=%s releaseIdentifier=%s source=project_reference_reuse sourceProjectId=%s sourcePath=%s",
                project.project_id,
                release_identifier,
                source_project_id,
                source_path,
            )
            action = _link_or_copy_reference_cog(source_path, target_cog_path)
            milestone.reference_imagery = _reference_imagery_from_cog_path(
                project_id=project.project_id,
                release_identifier=release_identifier,
                cog_path=target_cog_path,
                source_reference=source_reference,
            )
            repaired_count += 1
            logger.info(
                "TEMPORAL_REFERENCE_METADATA_REGISTERED projectId=%s releaseIdentifier=%s cogPath=%s tilejsonUrl=%s",
                project.project_id,
                release_identifier,
                milestone.reference_imagery.cog_path if milestone.reference_imagery else target_cog_path,
                milestone.reference_imagery.tilejson_url if milestone.reference_imagery else None,
            )
            logger.info(
                "REFERENCE_REUSE_COG_%s projectId=%s releaseIdentifier=%s aoiHash=%s sourceProjectId=%s sourcePath=%s targetPath=%s durationMs=%s",
                "CANONICAL_CACHE_MATERIALIZED"
                if action == "canonical_cache_materialized"
                else ("LINKED" if action == "linked" else "COPIED"),
                project.project_id,
                release_identifier,
                aoi_hash,
                source_project_id,
                source_path,
                target_cog_path,
                round((time.perf_counter() - started_at) * 1000, 2),
            )
            continue

        shared = _find_matching_shared_mosaic(
            settings=settings,
            project=project,
            release_identifier=release_identifier,
            aoi_hash=aoi_hash,
        )
        if shared is not None:
            source_raster_path, valid_mask_path = shared
            logger.info(
                "TEMPORAL_REFERENCE_SOURCE_RESOLVED projectId=%s releaseIdentifier=%s source=shared_wayback_mosaic sourcePath=%s validMaskPath=%s",
                project.project_id,
                release_identifier,
                source_raster_path,
                valid_mask_path,
            )
            reusable, reusable_reason = _reference_raster_is_project_reusable(source_raster_path)
            if reusable and not settings.reference_imagery_cache_enabled:
                action = _link_or_copy_reference_cog(source_raster_path, target_cog_path)
                logger.info(
                    "TEMPORAL_REFERENCE_COG_WRITTEN projectId=%s releaseIdentifier=%s action=%s reason=%s sourcePath=%s cogPath=%s",
                    project.project_id,
                    release_identifier,
                    action,
                    reusable_reason,
                    source_raster_path,
                    target_cog_path,
                )
                reference = _reference_imagery_from_cog_path(
                    project_id=project.project_id,
                    release_identifier=release_identifier,
                    cog_path=target_cog_path,
                    source_reference=milestone.reference_imagery,
                )
            else:
                if reusable:
                    logger.info(
                        "TEMPORAL_REFERENCE_COG_WRITTEN projectId=%s releaseIdentifier=%s action=canonical_cache_materialize reason=%s sourcePath=%s cogPath=%s",
                        project.project_id,
                        release_identifier,
                        reusable_reason,
                        source_raster_path,
                        target_cog_path,
                    )
                reference = build_temporal_reference_imagery(
                    project_id=project.project_id,
                    project_dir=project_dir,
                    release_identifier=release_identifier,
                    source=TemporalReferenceSource(
                        image_path=None,
                        image_png_data_url=None,
                        raster_bounds_wgs84=None,
                        source_raster_path=str(source_raster_path),
                        valid_mask_path=str(valid_mask_path) if valid_mask_path else None,
                        aoi_geojson=project.aoi_geojson,
                    ),
                    settings=settings,
                )
            if reference and reference.cog_path:
                milestone.reference_imagery = reference
                repaired_count += 1
                logger.info(
                    "TEMPORAL_REFERENCE_METADATA_REGISTERED projectId=%s releaseIdentifier=%s cogPath=%s tilejsonUrl=%s",
                    project.project_id,
                    release_identifier,
                    reference.cog_path,
                    reference.tilejson_url,
                )
                if release_identifier == (project.milestones[0].release_identifier if project.milestones else None):
                    logger.info(
                        "REFERENCE_BASELINE_IMAGERY_COG_GENERATED projectId=%s releaseIdentifier=%s aoiHash=%s sourcePath=%s targetPath=%s",
                        project.project_id,
                        release_identifier,
                        aoi_hash,
                        source_raster_path,
                        reference.cog_path,
                    )
                    logger.info(
                        "REFERENCE_BASELINE_IMAGERY_ATTACHED projectId=%s releaseIdentifier=%s aoiHash=%s targetPath=%s",
                        project.project_id,
                        release_identifier,
                        aoi_hash,
                        reference.cog_path,
                    )
                logger.info(
                    "REFERENCE_REUSE_COG_GENERATED_FROM_SHARED_MOSAIC projectId=%s releaseIdentifier=%s aoiHash=%s sourcePath=%s targetPath=%s durationMs=%s",
                    project.project_id,
                    release_identifier,
                    aoi_hash,
                    source_raster_path,
                    reference.cog_path,
                    round((time.perf_counter() - started_at) * 1000, 2),
                )
                continue

        logger.info(
            "REFERENCE_REUSE_NO_MATCH projectId=%s releaseIdentifier=%s aoiHash=%s reason=no_project_cog_or_shared_mosaic",
            project.project_id,
            release_identifier,
            aoi_hash,
        )

    if repaired_count:
        _persist_temporal_project_reference_repair(project, settings)
    logger.info(
        "REFERENCE_LAYERS_REPAIR_DONE projectId=%s count=%s aoiHash=%s durationMs=%s reason=%s",
        project.project_id,
        repaired_count,
        aoi_hash,
        round((time.perf_counter() - started_at) * 1000, 2),
        "repaired" if repaired_count else "no_reference_imagery_source",
    )
    return project, repaired_count


def _reference_cog_metadata(cog_path: Path) -> dict[str, Any]:
    with rasterio.open(cog_path) as src:
        compression = src.compression.value if src.compression is not None else None
        return {
            "path": str(cog_path),
            "size_bytes": cog_path.stat().st_size,
            "crs": str(src.crs) if src.crs else None,
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "compression": compression,
            "bounds": [src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top],
        }


def repair_temporal_project_reference_imagery_for_publication(
    project_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Repair milestone COG references and return validation metadata for publication scripts."""

    project, repaired_count = repair_temporal_project_reference_imagery(project_id, settings)
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    milestones: dict[str, dict[str, Any]] = {}
    available_count = 0
    for milestone in project.milestones:
        release_identifier = milestone.release_identifier
        cog_path = project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
        if not cog_path.is_file():
            logger.info(
                "TEMPORAL_PROJECT_REFERENCE_COG_MISSING projectId=%s releaseIdentifier=%s path=%s",
                project.project_id,
                release_identifier,
                cog_path,
            )
            milestones[release_identifier] = {
                "available": False,
                "path": str(cog_path),
                "reason": "missing_cog",
            }
            continue
        try:
            metadata = _reference_cog_metadata(cog_path)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "TEMPORAL_PROJECT_REFERENCE_COG_MISSING projectId=%s releaseIdentifier=%s path=%s reason=invalid_cog error=%s",
                project.project_id,
                release_identifier,
                cog_path,
                exc.__class__.__name__,
            )
            milestones[release_identifier] = {
                "available": False,
                "path": str(cog_path),
                "reason": "invalid_cog",
                "error": str(exc),
            }
            continue
        available_count += 1
        milestones[release_identifier] = {"available": True, **metadata}
        logger.info(
            "TEMPORAL_PROJECT_REFERENCE_COG_VALIDATED projectId=%s releaseIdentifier=%s path=%s sizeBytes=%s crs=%s width=%s height=%s compression=%s",
            project.project_id,
            release_identifier,
            cog_path,
            metadata["size_bytes"],
            metadata["crs"],
            metadata["width"],
            metadata["height"],
            metadata["compression"],
        )

    logger.info(
        "TEMPORAL_PROJECT_REFERENCE_IMAGERY_AVAILABLE projectId=%s availableCount=%s milestoneCount=%s repairedCount=%s",
        project.project_id,
        available_count,
        len(project.milestones),
        repaired_count,
    )
    return {
        "project_id": project.project_id,
        "repaired_count": repaired_count,
        "available_count": available_count,
        "milestone_count": len(project.milestones),
        "milestones": milestones,
    }


def _temporal_reference_imagery_is_complete(
    reference: TemporalReferenceImagery | None,
    *,
    fallback_cog_path: Path,
) -> bool:
    if reference is None:
        return False
    cog_path = Path(reference.cog_path).expanduser() if reference.cog_path else fallback_cog_path
    return (
        cog_path.is_file()
        and bool(reference.tilejson_url)
        and bool(reference.tiles_url_template)
        and bool(reference.reference_imagery_key)
        and bool(reference.canonical_cog_path)
    )


def _ensure_temporal_project_reference_imagery_from_canonical_cache(
    *,
    project: TemporalProject,
    settings: Settings,
    project_dir: Path,
) -> int:
    """Materialize exact canonical reference COGs into a temporal project.

    This is intentionally narrower than the manual repair path: it only uses an
    unambiguous canonical cache match keyed by the project AOI hash and milestone
    release identifier. That keeps normal project finalization deterministic and
    avoids broad project/shared-mosaic scans during every save.
    """

    if project.aoi_geojson is None:
        return 0

    aoi_hash = _temporal_reference_aoi_hash(project.aoi_geojson)
    materialized_count = 0
    for milestone in project.milestones:
        release_identifier = milestone.release_identifier
        target_cog_path = project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
        if _temporal_reference_imagery_is_complete(milestone.reference_imagery, fallback_cog_path=target_cog_path):
            continue

        if target_cog_path.is_file():
            canonical_match = _find_matching_canonical_reference_imagery(
                settings=settings,
                release_identifier=release_identifier,
                aoi_hash=aoi_hash,
            )
            canonical_cog_path = canonical_match[0] if canonical_match is not None else None
            canonical_metadata = canonical_match[1] if canonical_match is not None else {}
            reference_imagery_key = (
                str(canonical_metadata.get("reference_imagery_key"))
                if canonical_metadata.get("reference_imagery_key")
                else (milestone.reference_imagery.reference_imagery_key if milestone.reference_imagery else None)
            )
            if canonical_match is not None:
                append_reference_imagery_materialization(
                    canonical_metadata,
                    project_id=project.project_id,
                    release_identifier=release_identifier,
                    project_cog_path=target_cog_path,
                    method="existing",
                )
                write_reference_imagery_cache_metadata(canonical_cog_path.with_name("metadata.json"), canonical_metadata)
            milestone.reference_imagery = _reference_imagery_from_cog_path(
                project_id=project.project_id,
                release_identifier=release_identifier,
                cog_path=target_cog_path,
                source_reference=milestone.reference_imagery,
                reference_imagery_key=reference_imagery_key,
                canonical_cog_path=canonical_cog_path
                or (milestone.reference_imagery.canonical_cog_path if milestone.reference_imagery else None),
                materialization_method=milestone.reference_imagery.materialization_method
                if milestone.reference_imagery
                else "existing",
            )
            materialized_count += 1
            logger.info(
                "TEMPORAL_REFERENCE_FINALIZATION_METADATA_COMPLETED projectId=%s releaseIdentifier=%s source=project_local_cog cogPath=%s canonicalCogPath=%s",
                project.project_id,
                release_identifier,
                target_cog_path,
                canonical_cog_path,
            )
            continue

        canonical_match = _find_matching_canonical_reference_imagery(
            settings=settings,
            release_identifier=release_identifier,
            aoi_hash=aoi_hash,
        )
        if canonical_match is None:
            logger.info(
                "TEMPORAL_REFERENCE_FINALIZATION_NO_CANONICAL_MATCH projectId=%s releaseIdentifier=%s aoiHash=%s",
                project.project_id,
                release_identifier,
                aoi_hash,
            )
            continue

        canonical_cog_path, canonical_metadata = canonical_match
        materialization = materialize_reference_imagery_cog(
            canonical_cog_path=canonical_cog_path,
            project_cog_path=target_cog_path,
            mode=settings.reference_imagery_materialization,
        )
        method = str(materialization.get("method") or "unknown")
        append_reference_imagery_materialization(
            canonical_metadata,
            project_id=project.project_id,
            release_identifier=release_identifier,
            project_cog_path=target_cog_path,
            method=method,
        )
        write_reference_imagery_cache_metadata(canonical_cog_path.with_name("metadata.json"), canonical_metadata)
        milestone.reference_imagery = _reference_imagery_from_cog_path(
            project_id=project.project_id,
            release_identifier=release_identifier,
            cog_path=target_cog_path,
            source_reference=milestone.reference_imagery,
            reference_imagery_key=(
                str(canonical_metadata.get("reference_imagery_key"))
                if canonical_metadata.get("reference_imagery_key")
                else None
            ),
            canonical_cog_path=canonical_cog_path,
            materialization_method=method,
        )
        materialized_count += 1
        logger.info(
            "TEMPORAL_REFERENCE_FINALIZATION_MATERIALIZED projectId=%s releaseIdentifier=%s referenceImageryKey=%s method=%s canonicalCogPath=%s projectCogPath=%s",
            project.project_id,
            release_identifier,
            canonical_metadata.get("reference_imagery_key"),
            method,
            canonical_cog_path,
            target_cog_path,
        )
    return materialized_count


def _load_geojson_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _geojson_property_area_m2(payload: dict[str, Any] | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    total = 0.0
    seen = False
    for feature in payload.get("features", []) or []:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            continue
        value = properties.get("area_m2") or properties.get("area_sqm") or properties.get("area")
        if value is None:
            continue
        try:
            total += float(value)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def _geojson_union_area_m2(payload: dict[str, Any] | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    geometry = _geometry_from_geojson(payload)
    if geometry.is_empty:
        return 0.0
    return geodesic_area_m2(geometry)


def _audit_metric_layer(
    *,
    project_id: str,
    release_identifier: str,
    layer_key: str,
    path: Path,
) -> dict[str, Any]:
    payload = _load_geojson_file(path)
    feature_count = len(payload.get("features", [])) if isinstance(payload, dict) and isinstance(payload.get("features"), list) else 0
    property_area = _geojson_property_area_m2(payload)
    geometry_area = _geojson_union_area_m2(payload)
    layer_result = {
        "path": str(path),
        "feature_count": feature_count,
        "property_area_m2": round(property_area, 2) if property_area is not None else None,
        "geometry_area_m2": round(geometry_area, 2) if geometry_area is not None else None,
        "geometry_area_km2": round(geometry_area / 1_000_000, 6) if geometry_area is not None else None,
    }
    logger.info(
        "TEMPORAL_PROJECT_METRIC_AUDIT_LAYER projectId=%s releaseIdentifier=%s layer=%s path=%s featureCount=%s propertyAreaM2=%s geometryAreaM2=%s",
        project_id,
        release_identifier,
        layer_key,
        path,
        layer_result["feature_count"],
        layer_result["property_area_m2"],
        layer_result["geometry_area_m2"],
    )
    return layer_result


def audit_temporal_project_metrics(
    *,
    project_id: str,
    target_release: str,
    settings: Settings,
) -> dict[str, Any]:
    logger.info(
        "TEMPORAL_PROJECT_METRIC_AUDIT_START projectId=%s targetRelease=%s",
        project_id,
        target_release,
    )
    project = _load_project(
        settings,
        project_id,
        hydrate_reference_imagery=False,
        hydrate_buffer_layers=False,
        refresh_derived_layers=False,
        write_side_effects=False,
    )
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    milestone = next((item for item in project.milestones if item.release_identifier == target_release), None)
    if milestone is None:
        raise ValueError(f"Target release {target_release} is not present in project {project_id}.")
    milestone_dir = project_dir / "milestones" / target_release
    layers = {
        "additions": _audit_metric_layer(
            project_id=project.project_id,
            release_identifier=target_release,
            layer_key="additions",
            path=milestone_dir / "additions.geojson",
        ),
    }
    metrics_payload = milestone.metrics.model_dump(mode="json") if milestone.metrics is not None else {}
    ui_added_area_m2 = float(metrics_payload.get("added_area_m2") or 0.0)
    additions_geometry_area = layers["additions"].get("geometry_area_m2")
    mismatch = (
        additions_geometry_area is not None
        and abs(float(additions_geometry_area) - ui_added_area_m2) > 1.0
    )
    if mismatch:
        logger.warning(
            "TEMPORAL_PROJECT_METRIC_MISMATCH projectId=%s releaseIdentifier=%s uiAddedAreaM2=%s additionsGeometryAreaM2=%s",
            project.project_id,
            target_release,
            ui_added_area_m2,
            additions_geometry_area,
        )
    result = {
        "project_id": project.project_id,
        "target_release": target_release,
        "metrics": metrics_payload,
        "ui_added_area_m2": round(ui_added_area_m2, 2),
        "ui_added_area_km2": round(ui_added_area_m2 / 1_000_000, 6),
        "layers": layers,
        "mismatch": mismatch,
    }
    logger.info(
        "TEMPORAL_PROJECT_METRIC_AUDIT_DONE projectId=%s targetRelease=%s uiAddedAreaM2=%s uiAddedAreaKm2=%s mismatch=%s",
        project.project_id,
        target_release,
        result["ui_added_area_m2"],
        result["ui_added_area_km2"],
        mismatch,
    )
    return result


def audit_temporal_project_metadata_bloat(
    *,
    project_id: str,
    settings: Settings,
    repair_metadata: bool = False,
    threshold_bytes: int = 100_000_000,
) -> dict[str, Any]:
    project_dir = _resolve_project_dir(settings, project_id, None)
    project_json_path = project_dir / "project.json"
    manifest_path = project_dir / "project_manifest.json"
    project_size = project_json_path.stat().st_size if project_json_path.is_file() else 0
    manifest_size = manifest_path.stat().st_size if manifest_path.is_file() else 0
    bloated = project_size > threshold_bytes or manifest_size > threshold_bytes
    result = {
        "project_id": project_id,
        "project_json": str(project_json_path),
        "project_json_size_bytes": project_size,
        "project_manifest": str(manifest_path),
        "project_manifest_size_bytes": manifest_size,
        "threshold_bytes": threshold_bytes,
        "bloated": bloated,
        "repair_metadata_requested": repair_metadata,
        "repair_metadata_applied": False,
        "reason": None,
    }
    if bloated:
        logger.warning(
            "TEMPORAL_PROJECT_METADATA_BLOAT_DETECTED projectId=%s projectJsonBytes=%s manifestBytes=%s thresholdBytes=%s likelyCause=embedded_feature_collections",
            project_id,
            project_size,
            manifest_size,
            threshold_bytes,
        )
    if repair_metadata:
        externalized = externalize_temporal_project_metadata(project_id=project_id, settings=settings, threshold_bytes=threshold_bytes)
        result.update(externalized)
        result["reason"] = "externalized_feature_collections"
        result["repair_metadata_applied"] = True
        logger.info(
            "TEMPORAL_PROJECT_METADATA_REFERENCES_WRITTEN projectId=%s written=true reason=%s projectJsonBeforeBytes=%s projectJsonAfterBytes=%s manifestBeforeBytes=%s manifestAfterBytes=%s",
            project_id,
            result["reason"],
            externalized["project_json_before_bytes"],
            externalized["project_json_after_bytes"],
            externalized["project_manifest_before_bytes"],
            externalized["project_manifest_after_bytes"],
        )
    return result


def externalize_temporal_project_metadata(
    *,
    project_id: str,
    settings: Settings,
    threshold_bytes: int = 100_000_000,
) -> dict[str, Any]:
    project = _load_project(
        settings,
        project_id,
        hydrate_reference_imagery=False,
        hydrate_buffer_layers=False,
        refresh_derived_layers=False,
        write_side_effects=False,
    )
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    project.project_dir = str(project_dir)
    before_project_json = project_dir / "project.json"
    before_manifest = project_dir / "project_manifest.json"
    before_project_size = before_project_json.stat().st_size if before_project_json.is_file() else 0
    before_manifest_size = before_manifest.stat().st_size if before_manifest.is_file() else 0
    externalized_count = 0
    bytes_externalized = 0
    baseline_release_identifier = project.milestones[0].release_identifier if project.milestones else None
    empty_baseline_artifacts_removed = 0
    for milestone in project.milestones:
        artifacts_by_key = {artifact.key: artifact for artifact in milestone.artifacts if artifact.key in TEMPORAL_ALLOWED_ARTIFACT_KEYS}
        for artifact_key, (field_path, filename, description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, filename)
            payload = _artifact_payload_for_milestone(milestone, field_path)
            if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                written_path = _write_geojson(artifact_path, payload)
                if written_path:
                    bytes_externalized += artifact_path.stat().st_size
                    externalized_count += 1
            if artifact_path.is_file():
                size_bytes = artifact_path.stat().st_size
                compute_geojson_metadata = media_type != "application/geo+json" or size_bytes < TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES
                artifact_entry = _temporal_artifact_entry(
                    project_id=project.project_id,
                    release_identifier=milestone.release_identifier,
                    artifact_key=artifact_key,
                    path=artifact_path,
                    description=description,
                    media_type=media_type,
                    compute_geojson_metadata=compute_geojson_metadata,
                )
                if (
                    milestone.release_identifier == baseline_release_identifier
                    and media_type == "application/geo+json"
                    and compute_geojson_metadata
                    and (artifact_entry.feature_count or 0) == 0
                ):
                    artifacts_by_key.pop(artifact_key, None)
                    empty_baseline_artifacts_removed += 1
                    logger.info(
                        "BASELINE_EMPTY_OUTPUT_ARTIFACT_FILTERED projectId=%s releaseIdentifier=%s artifactKey=%s path=%s",
                        project.project_id,
                        milestone.release_identifier,
                        artifact_key,
                        artifact_path,
                    )
                else:
                    artifacts_by_key[artifact_key] = artifact_entry
            _clear_artifact_payload_for_milestone(milestone, field_path)
        milestone.artifacts = list(artifacts_by_key.values())
    project.updated_at = _utc_now_iso()
    payload = project.model_dump(mode="json")
    before_project_json.write_text(json.dumps(payload, indent=2))
    before_manifest.write_text(json.dumps(payload, indent=2))
    _write_project_summary(project, before_project_json)
    after_project_size = before_project_json.stat().st_size
    after_manifest_size = before_manifest.stat().st_size
    logger.info(
        "PROJECT_METADATA_EXTERNALIZED projectId=%s externalizedArtifacts=%s bytesExternalized=%s emptyBaselineArtifactsRemoved=%s projectJsonBefore=%s projectJsonAfter=%s manifestBefore=%s manifestAfter=%s",
        project.project_id,
        externalized_count,
        bytes_externalized,
        empty_baseline_artifacts_removed,
        before_project_size,
        after_project_size,
        before_manifest_size,
        after_manifest_size,
    )
    if after_project_size > threshold_bytes or after_manifest_size > threshold_bytes:
        logger.warning(
            "PROJECT_METADATA_BLOAT_DETECTED projectId=%s projectJsonBytes=%s manifestBytes=%s thresholdBytes=%s phase=after_externalize",
            project.project_id,
            after_project_size,
            after_manifest_size,
            threshold_bytes,
        )
    return {
        "project_id": project.project_id,
        "externalized_artifacts": externalized_count,
        "bytes_externalized": bytes_externalized,
        "empty_baseline_artifacts_removed": empty_baseline_artifacts_removed,
        "project_json_before_bytes": before_project_size,
        "project_json_after_bytes": after_project_size,
        "project_manifest_before_bytes": before_manifest_size,
        "project_manifest_after_bytes": after_manifest_size,
        "project_summary": str(_project_summary_json_path(before_project_json)),
    }


def resolve_temporal_project_artifact_path(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    settings: Settings,
    access_mode: str = "direct_download",
) -> tuple[Path, str]:
    if artifact_key.endswith(".gpkg"):
        artifact_key = artifact_key[: -len(".gpkg")]
        source_path, source_media_type = resolve_temporal_project_artifact_path(
            project_id=project_id,
            release_identifier=release_identifier,
            artifact_key=artifact_key,
            settings=settings,
            access_mode="qgis_gpkg_source",
        )
        if source_media_type != "application/geo+json":
            raise FileNotFoundError(f"GeoPackage export is only available for GeoJSON artifacts: {artifact_key}")
        feature_count, _bbox = _geojson_feature_count_and_bbox(source_path)
        source_size = source_path.stat().st_size
        if _is_empty_qgis_geojson_artifact(path=source_path, feature_count=feature_count, size_bytes=source_size):
            logger.info(
                "QGIS_GPKG_METADATA_SKIPPED_EMPTY release_identifier=%s artifact_key=%s reason=empty_artifact source_size_bytes=%s feature_count=%s",
                release_identifier,
                artifact_key,
                source_size,
                feature_count,
            )
            raise FileNotFoundError(f"GeoPackage export is not available for empty GeoJSON artifact: {artifact_key}")
        gpkg_path = ensure_temporal_project_artifact_gpkg(
            project_id=project_id,
            release_identifier=release_identifier,
            artifact_key=artifact_key,
            source_geojson_path=source_path,
            settings=settings,
        )
        return gpkg_path, TEMPORAL_QGIS_GPKG_MEDIA_TYPE
    if artifact_key.endswith(".geojson"):
        artifact_key = artifact_key[: -len(".geojson")]
    if artifact_key not in TEMPORAL_ALLOWED_ARTIFACT_KEYS or artifact_key not in TEMPORAL_LAYER_ARTIFACTS:
        raise FileNotFoundError(f"Unknown temporal artifact key: {artifact_key}")
    _, filename, _, media_type = TEMPORAL_LAYER_ARTIFACTS[artifact_key]
    project_dir = _resolve_project_dir(settings, project_id, None)
    project_dir = project_dir.resolve()
    path = (project_dir / "milestones" / release_identifier / filename).resolve()
    try:
        path.relative_to(project_dir)
    except ValueError as exc:
        raise FileNotFoundError(f"Invalid temporal artifact path: {artifact_key}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Temporal artifact not found: {artifact_key}")
    size_bytes = path.stat().st_size
    if access_mode == "vector_tile":
        logger.info(
            "PROJECT_LAYER_ARTIFACT_VECTOR_TILE_SOURCE_RESOLVED projectId=%s releaseIdentifier=%s artifactKey=%s path=%s bytes=%s",
            project_id,
            release_identifier,
            artifact_key,
            path,
            size_bytes,
        )
    elif access_mode.startswith("export"):
        logger.info(
            "EXPORT_ARTIFACT_RESOLVED projectId=%s releaseIdentifier=%s artifactKey=%s path=%s bytes=%s mediaType=%s accessMode=%s",
            project_id,
            release_identifier,
            artifact_key,
            path,
            size_bytes,
            media_type,
            access_mode,
        )
    elif access_mode == "qgis_gpkg_source":
        logger.info(
            "QGIS_GPKG_SOURCE_RESOLVED projectId=%s releaseIdentifier=%s artifactKey=%s path=%s bytes=%s",
            project_id,
            release_identifier,
            artifact_key,
            path,
            size_bytes,
        )
    else:
        logger.info(
            "PROJECT_LAYER_ARTIFACT_LAZY_FETCH projectId=%s releaseIdentifier=%s artifactKey=%s path=%s bytes=%s",
            project_id,
            release_identifier,
            artifact_key,
            path,
            size_bytes,
        )
    return path, media_type


def _qgis_gpkg_cache_path(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    source_geojson_path: Path,
    settings: Settings,
) -> Path:
    stat = source_geojson_path.stat()
    return (
        settings.runtime_cache_dir
        / "qgis_artifacts"
        / project_id
        / release_identifier
        / artifact_key
        / f"{stat.st_mtime_ns}-{stat.st_size}-{TEMPORAL_QGIS_GPKG_CONVERSION_VERSION}"
        / f"{artifact_key}.gpkg"
    )


def ensure_temporal_project_artifact_gpkg(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    source_geojson_path: Path,
    settings: Settings,
) -> Path:
    started_at = time.perf_counter()
    stat = source_geojson_path.stat()
    gpkg_path = _qgis_gpkg_cache_path(
        project_id=project_id,
        release_identifier=release_identifier,
        artifact_key=artifact_key,
        source_geojson_path=source_geojson_path,
        settings=settings,
    )
    logger.info(
        "QGIS_GPKG_REQUEST_START project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_path=%s source_size_bytes=%s source_mtime_ns=%s",
        project_id,
        release_identifier,
        artifact_key,
        source_geojson_path,
        gpkg_path,
        stat.st_size,
        stat.st_mtime_ns,
    )
    if gpkg_path.is_file() and gpkg_path.stat().st_size > 0:
        logger.info(
            "QGIS_GPKG_CACHE_HIT project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_path=%s source_size_bytes=%s source_mtime_ns=%s duration_ms=%s",
            project_id,
            release_identifier,
            artifact_key,
            source_geojson_path,
            gpkg_path,
            stat.st_size,
            stat.st_mtime_ns,
            round((time.perf_counter() - started_at) * 1000, 2),
        )
        return gpkg_path
    logger.info(
        "QGIS_GPKG_CACHE_MISS project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_path=%s source_size_bytes=%s source_mtime_ns=%s",
        project_id,
        release_identifier,
        artifact_key,
        source_geojson_path,
        gpkg_path,
        stat.st_size,
        stat.st_mtime_ns,
    )
    gpkg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = gpkg_path.with_suffix(".tmp.gpkg")
    if tmp_path.exists():
        tmp_path.unlink()
    command = [
        "ogr2ogr",
        "-f",
        "GPKG",
        str(tmp_path),
        str(source_geojson_path),
        "-nln",
        "results",
        "-lco",
        "SPATIAL_INDEX=YES",
    ]
    logger.info(
        "QGIS_GPKG_GENERATION_START project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_path=%s source_size_bytes=%s source_mtime_ns=%s",
        project_id,
        release_identifier,
        artifact_key,
        source_geojson_path,
        gpkg_path,
        stat.st_size,
        stat.st_mtime_ns,
    )
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        tmp_path.replace(gpkg_path)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        logger.exception(
            "QGIS_GPKG_GENERATION_FAILED project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_path=%s source_size_bytes=%s source_mtime_ns=%s duration_ms=%s error=%s",
            project_id,
            release_identifier,
            artifact_key,
            source_geojson_path,
            gpkg_path,
            stat.st_size,
            stat.st_mtime_ns,
            round((time.perf_counter() - started_at) * 1000, 2),
            exc,
        )
        raise
    logger.info(
        "QGIS_GPKG_GENERATION_DONE project_id=%s release_identifier=%s artifact_key=%s source_geojson_path=%s gpkg_path=%s source_size_bytes=%s source_mtime_ns=%s gpkg_size_bytes=%s duration_ms=%s",
        project_id,
        release_identifier,
        artifact_key,
        source_geojson_path,
        gpkg_path,
        stat.st_size,
        stat.st_mtime_ns,
        gpkg_path.stat().st_size,
        round((time.perf_counter() - started_at) * 1000, 2),
    )
    return gpkg_path


def _xyz_tile_bounds_wgs84(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z

    def tile_lon(tile_x: int) -> float:
        return tile_x / n * 360.0 - 180.0

    def tile_lat(tile_y: int) -> float:
        radians = math.atan(math.sinh(math.pi * (1 - 2 * tile_y / n)))
        return math.degrees(radians)

    west = tile_lon(x)
    east = tile_lon(x + 1)
    north = tile_lat(y)
    south = tile_lat(y + 1)
    return west, south, east, north


@lru_cache(maxsize=32)
def _load_geojson_index_for_vector_tiles(path: str, mtime_ns: int, size_bytes: int) -> TemporalVectorTileFeatureIndex:
    del mtime_ns, size_bytes
    payload = _load_geojson_file(Path(path))
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return TemporalVectorTileFeatureIndex(feature_count=0, bbox=None, geometries=(), properties=(), tree=STRtree([]))
    geometries: list[BaseGeometry] = []
    properties: list[dict[str, Any]] = []
    bounds: tuple[float, float, float, float] | None = None
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry_payload = feature.get("geometry")
        if not geometry_payload:
            continue
        try:
            geometry = shape(geometry_payload)
        except Exception:
            continue
        if geometry.is_empty:
            continue
        geometries.append(geometry)
        feature_properties = feature.get("properties")
        properties.append(feature_properties if isinstance(feature_properties, dict) else {})
        geom_bounds = geometry.bounds
        if bounds is None:
            bounds = geom_bounds
        else:
            bounds = (
                min(bounds[0], geom_bounds[0]),
                min(bounds[1], geom_bounds[1]),
                max(bounds[2], geom_bounds[2]),
                max(bounds[3], geom_bounds[3]),
            )
    return TemporalVectorTileFeatureIndex(
        feature_count=len(features),
        bbox=list(bounds) if bounds is not None else None,
        geometries=tuple(geometries),
        properties=tuple(properties),
        tree=STRtree(geometries),
    )


def build_temporal_artifact_vector_tilejson(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    settings: Settings,
    base_url: str | None = None,
) -> dict[str, Any]:
    path, media_type = resolve_temporal_project_artifact_path(
        project_id=project_id,
        release_identifier=release_identifier,
        artifact_key=artifact_key,
        settings=settings,
        access_mode="vector_tile",
    )
    if media_type != "application/geo+json":
        raise FileNotFoundError(f"Temporal artifact is not vector-tile capable: {artifact_key}")
    feature_count, bbox = _geojson_feature_count_and_bbox(path)
    tile_url = _temporal_vector_tiles_route(project_id, release_identifier, artifact_key)
    if base_url:
        tile_url = f"{base_url.rstrip('/')}{tile_url}"
    stat = path.stat()
    tile_url = f"{tile_url}?v={stat.st_mtime_ns}-{stat.st_size}"
    payload: dict[str, Any] = {
        "tilejson": "3.0.0",
        "name": f"{project_id}:{release_identifier}:{artifact_key}",
        "version": "1.0.0",
        "scheme": "xyz",
        "tiles": [tile_url],
        "minzoom": TEMPORAL_VECTOR_TILE_MINZOOM,
        "maxzoom": TEMPORAL_VECTOR_TILE_MAXZOOM,
        "vector_layers": [
            {
                "id": TEMPORAL_VECTOR_TILE_SOURCE_LAYER,
                "description": f"{release_identifier} {artifact_key}",
                "fields": {},
            }
        ],
        "feature_count": feature_count or 0,
    }
    if bbox:
        payload["bounds"] = bbox
    return payload


def render_temporal_artifact_vector_tile(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    z: int,
    x: int,
    y: int,
    settings: Settings,
) -> bytes:
    started_at = time.perf_counter()
    try:
        from mapbox_vector_tile import encode
    except ImportError as exc:  # pragma: no cover - dependency validation catches this.
        raise RuntimeError("mapbox-vector-tile is required for temporal vector tile exports.") from exc

    path, media_type = resolve_temporal_project_artifact_path(
        project_id=project_id,
        release_identifier=release_identifier,
        artifact_key=artifact_key,
        settings=settings,
        access_mode="vector_tile",
    )
    if media_type != "application/geo+json":
        raise FileNotFoundError(f"Temporal artifact is not vector-tile capable: {artifact_key}")
    stat = path.stat()
    tile_bounds = _xyz_tile_bounds_wgs84(z, x, y)
    tile_geom = box(*tile_bounds)
    source_index = _load_geojson_index_for_vector_tiles(str(path), stat.st_mtime_ns, stat.st_size)
    encoded_features: list[dict[str, Any]] = []
    candidate_indexes = source_index.tree.query(tile_geom)
    for candidate_index in candidate_indexes:
        geometry = source_index.geometries[int(candidate_index)]
        if geometry.is_empty or not geometry.intersects(tile_geom):
            continue
        clipped = geometry.intersection(tile_geom)
        if clipped.is_empty:
            continue
        properties = source_index.properties[int(candidate_index)]
        clipped_parts = clipped.geoms if isinstance(clipped, GeometryCollection) else (clipped,)
        for clipped_part in clipped_parts:
            if clipped_part.is_empty or clipped_part.geom_type not in {"Polygon", "MultiPolygon"}:
                continue
            encoded_features.append({"geometry": mapping(clipped_part), "properties": properties})
    logger.info(
        "TEMPORAL_ARTIFACT_VECTOR_TILE_RENDER projectId=%s releaseIdentifier=%s artifactKey=%s z=%s x=%s y=%s features=%s sourceFeatures=%s candidateFeatures=%s durationMs=%.2f",
        project_id,
        release_identifier,
        artifact_key,
        z,
        x,
        y,
        len(encoded_features),
        source_index.feature_count,
        len(candidate_indexes),
        (time.perf_counter() - started_at) * 1000,
    )
    if not encoded_features:
        return b""
    return encode(
        [{"name": TEMPORAL_VECTOR_TILE_SOURCE_LAYER, "features": encoded_features}],
        default_options={
            "quantize_bounds": tile_bounds,
            "extents": TEMPORAL_VECTOR_TILE_EXTENT,
        },
    )


def remove_empty_baseline_output_artifacts_from_metadata(*, project_id: str, settings: Settings) -> dict[str, Any]:
    project = _load_project(
        settings,
        project_id,
        hydrate_reference_imagery=False,
        hydrate_buffer_layers=False,
        refresh_derived_layers=False,
        write_side_effects=False,
    )
    if not project.milestones:
        return {"project_id": project_id, "removed": 0}
    baseline = project.milestones[0]
    before_count = len(baseline.artifacts)
    baseline.artifacts = [
        artifact
        for artifact in baseline.artifacts
        if not (
            artifact.key in TEMPORAL_LAYER_ARTIFACTS
            and artifact.media_type == "application/geo+json"
            and (artifact.feature_count or 0) == 0
        )
    ]
    removed = before_count - len(baseline.artifacts)
    if removed:
        project.updated_at = _utc_now_iso()
        project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
        payload = project.model_dump(mode="json")
        (project_dir / "project.json").write_text(json.dumps(payload, indent=2))
        (project_dir / "project_manifest.json").write_text(json.dumps(payload, indent=2))
        _write_project_summary(project, project_dir / "project.json")
    logger.info(
        "BASELINE_EMPTY_OUTPUT_ARTIFACTS_METADATA_FILTERED projectId=%s releaseIdentifier=%s removed=%s before=%s after=%s",
        project_id,
        baseline.release_identifier,
        removed,
        before_count,
        len(baseline.artifacts),
    )
    return {
        "project_id": project_id,
        "release_identifier": baseline.release_identifier,
        "removed": removed,
        "before": before_count,
        "after": len(baseline.artifacts),
    }


def temporal_reference_imagery_layers(project: TemporalProject) -> list[ReferenceLayer]:
    layers: list[ReferenceLayer] = []
    now = _utc_now_iso()
    for milestone in project.milestones:
        reference = milestone.reference_imagery
        if reference is None or not reference.cog_path:
            continue
        cog_path = Path(reference.cog_path)
        if not cog_path.is_file():
            continue
        layers.append(
            ReferenceLayer(
                layer_id=f"temporal-reference-{milestone.release_identifier}",
                project_id=project.project_id,
                name=f"{milestone.release_date or milestone.release_identifier} - reference imagery",
                original_filename="reference_imagery_cog.tif",
                original_format="geotiff",
                layer_kind="raster",
                geometry_type="raster",
                scope="aoi_clipped",
                storage_strategy="raster_tiles",
                crs=None,
                bounds_wgs84=reference.raster_bounds_wgs84,
                feature_count=None,
                file_size_bytes=cog_path.stat().st_size,
                source_path=str(cog_path),
                display_path=str(cog_path),
                display_url=reference.cog_url,
                pmtiles_url=None,
                tilejson_url=reference.tilejson_url,
                tiles_url_template=reference.tiles_url_template,
                source_layer=milestone.release_identifier,
                style=ReferenceLayerStyle(),
                visible=True,
                opacity=1.0,
                created_at=project.created_at or now,
                updated_at=project.updated_at or now,
                warnings=[],
            )
        )
    return layers


def _hydrate_reference_imagery(
    project: TemporalProject,
    settings: Settings,
    *,
    include_data_urls: bool = False,
) -> TemporalProject:
    milestones = project.milestones
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    for index, milestone in enumerate(milestones):
        reference_imagery = milestone.reference_imagery
        if reference_imagery is not None and reference_imagery.image_path:
            reference_imagery.image_png_data_url = None

        if milestone.pair_request_hash:
            reference_imagery = _reference_imagery_from_pair_response(
                load_cached_response(settings, milestone.pair_request_hash),
                project_id=project.project_id,
                project_dir=project_dir,
                release_identifier=milestone.release_identifier,
                use_t1_preview=False,
                aoi_geojson=project.aoi_geojson,
                include_data_url=include_data_urls,
                settings=settings,
            )

        if reference_imagery is None and index + 1 < len(milestones):
            next_pair_request_hash = milestones[index + 1].pair_request_hash
            if next_pair_request_hash:
                reference_imagery = _reference_imagery_from_pair_response(
                    load_cached_response(settings, next_pair_request_hash),
                    project_id=project.project_id,
                    project_dir=project_dir,
                    release_identifier=milestone.release_identifier,
                    use_t1_preview=True,
                    aoi_geojson=project.aoi_geojson,
                    include_data_url=include_data_urls,
                    settings=settings,
                )

        milestone.reference_imagery = reference_imagery

    return project


def _strip_redundant_reference_imagery_data_urls(project: TemporalProject) -> bool:
    changed = False
    for milestone in project.milestones:
        reference_imagery = milestone.reference_imagery
        if (
            reference_imagery is not None
            and reference_imagery.image_path
            and reference_imagery.image_png_data_url
        ):
            reference_imagery.image_png_data_url = None
            changed = True
    return changed


def _milestone_has_derived_geometry_layers(milestone: TemporalMilestone) -> bool:
    return (
        milestone.effective_building_blocks_geojson is not None
        and milestone.cumulative_growth_blocks_geojson is not None
        and milestone.metrics is not None
    )


def _ensure_temporal_derived_geometry_layers(project: TemporalProject, settings: Settings | None = None) -> TemporalProject:
    if all(_milestone_has_derived_geometry_layers(milestone) for milestone in project.milestones):
        return project
    return _refresh_temporal_derived_geometry_layers(project, settings)


def _hydrate_temporal_layer_artifacts(project: TemporalProject, settings: Settings) -> TemporalProject:
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    for milestone in project.milestones:
        artifacts_by_key = {artifact.key: artifact for artifact in milestone.artifacts if artifact.key in TEMPORAL_ALLOWED_ARTIFACT_KEYS}
        for artifact_key, (field_path, filename, _description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            if media_type != "application/geo+json" or _artifact_payload_for_milestone(milestone, field_path) is not None:
                continue
            artifact = artifacts_by_key.get(artifact_key)
            candidate = Path(artifact.path) if artifact is not None and artifact.path else _artifact_path_for_milestone(
                project_dir,
                milestone.release_identifier,
                filename,
            )
            try:
                candidate_size = candidate.stat().st_size
            except OSError:
                candidate_size = None
            if candidate_size is not None and candidate_size >= TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES:
                logger.info(
                    "TEMPORAL_ARTIFACT_HYDRATION_SKIPPED projectId=%s releaseIdentifier=%s artifactKey=%s reason=large_file_backed_artifact sizeBytes=%s",
                    project.project_id,
                    milestone.release_identifier,
                    artifact_key,
                    candidate_size,
                )
                continue
            payload = _load_geojson_file(candidate)
            if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
                continue
            if field_path.startswith("buffer_layers_geojson."):
                milestone.buffer_layers_geojson[field_path.split(".", 1)[1]] = payload
            else:
                setattr(milestone, field_path, payload)
    return project


def _load_file_backed_temporal_artifact(
    *,
    project: TemporalProject,
    project_dir: Path,
    milestone: TemporalMilestone,
    artifact_key: str,
    filename: str,
) -> dict[str, Any] | None:
    candidate_paths = [
        Path(artifact.path)
        for artifact in milestone.artifacts
        if artifact.key == artifact_key and artifact.path
    ]
    candidate_paths.append(_artifact_path_for_milestone(project_dir, milestone.release_identifier, filename))
    seen_paths: set[Path] = set()
    for candidate_path in candidate_paths:
        if candidate_path in seen_paths:
            continue
        seen_paths.add(candidate_path)
        payload = _load_geojson_file(candidate_path)
        if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
            logger.info(
                "TEMPORAL_OUTPUT_ARTIFACT_FOUND projectId=%s releaseIdentifier=%s artifactKey=%s source=file_backed_input path=%s featureCount=%s",
                project.project_id,
                milestone.release_identifier,
                artifact_key,
                candidate_path,
                _feature_count_from_geojson(payload),
            )
            return payload
    return None


def _repair_temporal_metrics_payload(
    *,
    project_id: str,
    payload: dict[str, Any],
    settings: Settings,
    project_dir: Path,
) -> bool:
    try:
        project = validate_stored_temporal_project(payload)
        project.project_dir = str(project_dir)
        project = _hydrate_temporal_layer_artifacts(project, settings)
        project = _refresh_temporal_derived_geometry_layers(project, settings)
    except Exception:
        logger.debug("TEMPORAL_METRICS_LOAD_TIME_REPAIR_FAILED projectId=%s", project_id, exc_info=True)
        return False

    milestones_by_release = {
        milestone.release_identifier: milestone
        for milestone in project.milestones
    }
    changed = False
    for milestone_payload in payload.get("milestones", []):
        if not isinstance(milestone_payload, dict):
            continue
        milestone = milestones_by_release.get(str(milestone_payload.get("release_identifier") or ""))
        if milestone is None or milestone.metrics is None:
            continue
        metrics_payload = milestone.metrics.model_dump(mode="json")
        if milestone_payload.get("metrics") != metrics_payload:
            milestone_payload["metrics"] = metrics_payload
            changed = True
    if changed:
        logger.info("TEMPORAL_METRICS_LOAD_TIME_REPAIR_DONE projectId=%s", project_id)
    return changed


def _hydrate_milestone_buffer_layers(project: TemporalProject, settings: Settings) -> TemporalProject:
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    logger.info("TEMPORAL_OUTPUT_ARTIFACT_DISCOVERY_START projectId=%s", project.project_id)
    for milestone in project.milestones:
        if milestone.buffer_layers_geojson:
            for key, payload in milestone.buffer_layers_geojson.items():
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_FEATURE_COUNT projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s featureCount=%s source=project_payload",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                    len(payload.get("features", [])) if isinstance(payload, dict) and isinstance(payload.get("features"), list) else 0,
                )
            continue
        if milestone.pair_request_hash:
            response = load_cached_response(settings, milestone.pair_request_hash)
        else:
            response = None
        if response is not None and response.buffer_layers_geojson:
            milestone.buffer_layers_geojson = response.buffer_layers_geojson
            for key, payload in milestone.buffer_layers_geojson.items():
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_FOUND projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s source=pair_response featureCount=%s",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                    len(payload.get("features", [])) if isinstance(payload, dict) and isinstance(payload.get("features"), list) else 0,
                )
            continue

        existing_layers: dict[str, dict[str, Any]] = {}
        existing_file_backed_layer_keys: set[str] = set()
        for key, filename in (
            ("10m", "building_change_buffer_10m.geojson"),
            ("15m", "building_change_buffer_15m.geojson"),
            ("20m", "building_change_buffer_20m.geojson"),
        ):
            artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, filename)
            if not artifact_path.is_file():
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_MISSING projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s reason=no_artifact_path path=%s",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                    artifact_path,
                )
                continue
            try:
                artifact_size = artifact_path.stat().st_size
            except OSError:
                artifact_size = None
            if artifact_size is not None and artifact_size >= TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES:
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_FOUND projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s source=milestone_file_backed path=%s sizeBytes=%s",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                    artifact_path,
                    artifact_size,
                )
                existing_file_backed_layer_keys.add(key)
                continue
            try:
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_MISSING projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s reason=invalid_geojson path=%s error=%s",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                    artifact_path,
                    exc.__class__.__name__,
                )
                continue
            if _has_features(payload):
                existing_layers[key] = payload
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_FOUND projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s source=milestone_file path=%s featureCount=%s",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                    artifact_path,
                    len(payload.get("features", [])),
                )
        if existing_layers:
            milestone.buffer_layers_geojson = existing_layers
        if set(existing_layers) | existing_file_backed_layer_keys == {"10m", "15m", "20m"}:
            continue

        additions = milestone.additions_geojson
        if not _has_features(additions):
            additions = _load_file_backed_temporal_artifact(
                project=project,
                project_dir=project_dir,
                milestone=milestone,
                artifact_key="additions",
                filename="additions.geojson",
            )
        if not _has_features(additions):
            logger.info(
                "TEMPORAL_OUTPUT_ARTIFACT_MISSING projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer reason=%s",
                project.project_id,
                milestone.release_identifier,
                "unsupported_for_baseline" if milestone == project.milestones[0] else "empty_geojson",
            )
            continue

        additions_feature_count = _feature_count_from_geojson(additions)

        previous_index = max(project.milestones.index(milestone) - 1, 0)
        previous_milestone = project.milestones[previous_index] if project.milestones else milestone
        try:
            logger.info(
                "TEMPORAL_BUFFER_GENERATION_START projectId=%s releaseIdentifier=%s sourceFeatureCount=%s distances=10m,15m,20m",
                project.project_id,
                milestone.release_identifier,
                additions_feature_count,
            )
            built_layers = build_change_buffer_layers(
                additions,
                distances_m=[10, 15, 20],
                context=VectorizationContext(
                    release_t1=previous_milestone.release_identifier,
                    release_t2=milestone.release_identifier,
                    src_date_t1=previous_milestone.release_date,
                    src_date_t2=milestone.release_date,
                ),
                keep_disjoint_parts_separate=True,
            )
        except Exception as exc:
            logger.warning(
                "TEMPORAL_OUTPUT_ARTIFACT_MISSING projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer reason=buffer_generation_failed error=%s",
                project.project_id,
                milestone.release_identifier,
                exc,
            )
            continue

        generated_layers: dict[str, dict[str, Any]] = {}
        for key, (_, payload) in built_layers.items():
            if not _has_features(payload):
                logger.info(
                    "TEMPORAL_OUTPUT_ARTIFACT_MISSING projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s reason=empty_geojson",
                    project.project_id,
                    milestone.release_identifier,
                    key,
                )
                continue
            filename = f"building_change_buffer_{key}.geojson"
            artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, filename)
            _write_geojson(artifact_path, payload)
            generated_layers[key] = payload
            logger.info(
                "TEMPORAL_OUTPUT_ARTIFACT_FOUND projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer_%s source=generated_from_additions path=%s featureCount=%s",
                project.project_id,
                milestone.release_identifier,
                key,
                artifact_path,
                len(payload.get("features", [])),
            )
        if generated_layers:
            milestone.buffer_layers_geojson = generated_layers
            logger.info(
                "TEMPORAL_BUFFER_GENERATION_DONE projectId=%s releaseIdentifier=%s generatedLayers=%s",
                project.project_id,
                milestone.release_identifier,
                ",".join(sorted(generated_layers.keys())),
            )

    logger.info(
        "TEMPORAL_OUTPUT_LAYER_AVAILABILITY_BUILT projectId=%s milestoneCount=%s",
        project.project_id,
        len(project.milestones),
    )
    return project


def _refresh_temporal_derived_geometry_layers(project: TemporalProject, settings: Settings | None = None) -> TemporalProject:
    if project.aoi_geojson is None:
        return project

    for milestone in project.milestones:
        max_features = settings.temporal_derived_geometry_max_features if settings is not None else None
        additions_feature_count = _feature_count_from_geojson(milestone.additions_geojson)
        cumulative_feature_count = _feature_count_from_geojson(milestone.cumulative_union_geojson)
        if max_features is not None and max(additions_feature_count, cumulative_feature_count) > max_features:
            logger.info(
                "TEMPORAL_DERIVED_GEOMETRY_REFRESH_SKIPPED projectId=%s releaseIdentifier=%s reason=optional_large_result_refresh_skipped additionsFeatureCount=%s cumulativeFeatureCount=%s maxInlineDerivedFeatures=%s",
                project.project_id,
                milestone.release_identifier,
                additions_feature_count,
                cumulative_feature_count,
                max_features,
            )
            logger.info(
                "TEMPORAL_OPTIONAL_EXPORT_SKIPPED_LARGE_RESULT projectId=%s releaseIdentifier=%s artifactKey=temporal_derived_geometry reason=inline_growth_layers_too_large additionsFeatureCount=%s cumulativeFeatureCount=%s maxInlineDerivedFeatures=%s",
                project.project_id,
                milestone.release_identifier,
                additions_feature_count,
                cumulative_feature_count,
                max_features,
            )
            if milestone.metrics is None:
                milestone.metrics = TemporalMilestoneMetrics(
                    additions_feature_count=additions_feature_count,
                    effective_feature_count=cumulative_feature_count or additions_feature_count,
                )
            else:
                milestone.metrics.growth_envelope_area_m2 = 0.0
            milestone.cumulative_growth_envelope_geojson = None
            _disable_temporal_growth_envelope(project.project_id, milestone.release_identifier)
            continue

        release_date = milestone.release_date
        if milestone.additions_geojson is not None:
            _, milestone.effective_building_blocks_geojson = build_temporal_growth_blocks(
                milestone.additions_geojson,
                aoi_geojson=project.aoi_geojson,
                release_identifier=milestone.release_identifier,
                release_date=release_date,
                kind="effective_building_block",
            )
        if milestone.cumulative_union_geojson is not None:
            _, milestone.cumulative_growth_blocks_geojson = build_temporal_growth_blocks(
                milestone.cumulative_union_geojson,
                aoi_geojson=project.aoi_geojson,
                release_identifier=milestone.release_identifier,
                release_date=release_date,
                kind="cumulative_growth_block",
            )
            milestone.cumulative_growth_envelope_geojson = None
            _disable_temporal_growth_envelope(project.project_id, milestone.release_identifier)
        additions_geometry = _geometry_from_geojson(milestone.additions_geojson)
        effective_geometry = _geometry_from_geojson(milestone.cumulative_union_geojson)
        if not additions_geometry.is_empty or not effective_geometry.is_empty:
            previous_metrics = milestone.metrics
            refreshed_metrics = _build_metrics(
                additions_geometry,
                effective_geometry,
                building_level_available=milestone.manual_override_geojson is None,
                effective_building_blocks_geojson=milestone.effective_building_blocks_geojson,
                cumulative_growth_blocks_geojson=milestone.cumulative_growth_blocks_geojson,
                cumulative_growth_envelope_geojson=None,
            )
            if previous_metrics is not None and milestone.additions_geojson is None:
                refreshed_metrics.added_area_m2 = previous_metrics.added_area_m2
                refreshed_metrics.additions_feature_count = previous_metrics.additions_feature_count
                refreshed_metrics.added_block_count = previous_metrics.added_block_count
                refreshed_metrics.added_block_area_m2 = previous_metrics.added_block_area_m2
            if previous_metrics is not None and milestone.cumulative_union_geojson is None:
                refreshed_metrics.total_area_m2 = previous_metrics.total_area_m2
                refreshed_metrics.effective_feature_count = previous_metrics.effective_feature_count
                refreshed_metrics.cumulative_block_count = previous_metrics.cumulative_block_count
                refreshed_metrics.cumulative_block_area_m2 = previous_metrics.cumulative_block_area_m2
                refreshed_metrics.growth_envelope_area_m2 = 0.0
            milestone.metrics = refreshed_metrics
    return project


def _refresh_project_bundle(project: TemporalProject, settings: Settings) -> TemporalProject:
    started_at = time.perf_counter()
    logger.info(
        "TEMPORAL_PROJECT_PUBLICATION_START projectId=%s milestoneCount=%s",
        project.project_id,
        len(project.milestones),
    )
    project = _hydrate_reference_imagery(project, settings)
    project = _hydrate_temporal_layer_artifacts(project, settings)
    project = _hydrate_milestone_buffer_layers(project, settings)
    project = _refresh_temporal_derived_geometry_layers(project, settings)
    _strip_large_inline_temporal_result_payloads(project, settings)
    logger.info(
        "TEMPORAL_PROJECT_PUBLICATION_USING_EXISTING_FINALIZER projectId=%s finalizer=_refresh_project_bundle",
        project.project_id,
    )
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    bundle_path = project_dir / "temporal_project_bundle.zip"
    manifest_path = project_dir / "project_manifest.json"

    for milestone in project.milestones:
        milestone_artifacts: list[TemporalArtifactEntry] = []
        cached_pair_response = load_cached_response(settings, milestone.pair_request_hash) if milestone.pair_request_hash else None
        for name, description, payload in (
            ("automated_building_blocks.geojson", "Automated building-level blocks", milestone.automated_building_blocks_geojson),
            ("additions.geojson", "Effective additions since previous milestone", milestone.additions_geojson),
            ("building_change_buffer_10m.geojson", "Building-change buffer 10 m", milestone.buffer_layers_geojson.get("10m")),
            ("building_change_buffer_15m.geojson", "Building-change buffer 15 m", milestone.buffer_layers_geojson.get("15m")),
            ("building_change_buffer_20m.geojson", "Building-change buffer 20 m", milestone.buffer_layers_geojson.get("20m")),
        ):
            artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, name)
            if payload is None:
                if artifact_path.is_file() and artifact_path.stat().st_size > 0:
                    logger.info(
                        "TEMPORAL_PROJECT_PUBLICATION_LAYER_REGISTERED projectId=%s releaseIdentifier=%s filename=%s path=%s source=existing_file_backed sizeBytes=%s",
                        project.project_id,
                        milestone.release_identifier,
                        name,
                        artifact_path,
                        artifact_path.stat().st_size,
                    )
                    milestone_artifacts.append(
                        TemporalArtifactEntry(
                            name=f"{milestone.release_identifier}_{name.replace('.geojson', '')}",
                            path=str(artifact_path),
                            media_type="application/geo+json",
                            description=description,
                        )
                    )
                continue
            written_path = _write_geojson(artifact_path, payload)
            if written_path:
                feature_count = len(payload.get("features", [])) if isinstance(payload, dict) and isinstance(payload.get("features"), list) else 0
                logger.info(
                    "TEMPORAL_PROJECT_PUBLICATION_LAYER_WRITTEN projectId=%s releaseIdentifier=%s filename=%s path=%s featureCount=%s",
                    project.project_id,
                    milestone.release_identifier,
                    name,
                    written_path,
                    feature_count,
                )
                milestone_artifacts.append(
                    TemporalArtifactEntry(
                        name=f"{milestone.release_identifier}_{name.replace('.geojson', '')}",
                        path=written_path,
                        media_type="application/geo+json",
                        description=description,
                    )
                )

        milestone.artifacts = milestone_artifacts

    externalized_count, empty_baseline_artifacts_removed = _externalize_temporal_artifact_payloads_in_project(
        project=project,
        project_dir=project_dir,
    )
    if externalized_count or empty_baseline_artifacts_removed:
        logger.info(
            "TEMPORAL_PROJECT_METADATA_PAYLOAD_EXTERNALIZED projectId=%s source=project_bundle externalizedArtifacts=%s emptyBaselineArtifactsRemoved=%s",
            project.project_id,
            externalized_count,
            empty_baseline_artifacts_removed,
        )
    manifest_payload = project.model_dump(mode="json")
    manifest_path.write_text(json.dumps(manifest_payload, indent=2))
    logger.info(
        "TEMPORAL_PROJECT_PUBLICATION_MANIFEST_UPDATED projectId=%s path=%s",
        project.project_id,
        manifest_path,
    )
    project.download_bundle_path = str(bundle_path) if bundle_path.exists() else None
    logger.info(
        "TEMPORAL_PROJECT_PUBLICATION_DONE projectId=%s durationMs=%s",
        project.project_id,
        round((time.perf_counter() - started_at) * 1000, 2),
    )
    return project


def _safe_export_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned or "projet_temporel"


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _month_label_fr(year_month: str) -> str:
    year_str, month_str = year_month.split("-", 1)
    month_name = calendar.month_name[max(1, min(12, int(month_str)))]
    fr_month = {
        "January": "janvier",
        "February": "février",
        "March": "mars",
        "April": "avril",
        "May": "mai",
        "June": "juin",
        "July": "juillet",
        "August": "août",
        "September": "septembre",
        "October": "octobre",
        "November": "novembre",
        "December": "décembre",
    }.get(month_name, month_str)
    return f"{fr_month} {year_str}"


def _milestone_source_year_month(
    milestone: TemporalMilestone,
    *,
    pair_dir: Path | None,
    export_now: datetime,
) -> tuple[str, str]:
    if pair_dir is not None:
        summary_path = pair_dir / "wayback_pair_summary.csv"
        if summary_path.exists():
            try:
                with summary_path.open("r", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        if row.get("label") == "t2":
                            dominant_src_date = (row.get("dominant_src_date") or "").strip()
                            parsed = _parse_iso_date(dominant_src_date)
                            if parsed is not None:
                                ym = parsed.strftime("%Y-%m")
                                return ym, _month_label_fr(ym)
            except Exception:
                pass

    parsed_release = _parse_iso_date(milestone.release_date)
    if parsed_release is not None:
        ym = parsed_release.strftime("%Y-%m")
        return ym, _month_label_fr(ym)

    identifier_match = re.search(r"(20\d{2})", milestone.release_identifier)
    if identifier_match:
        ym = f"{identifier_match.group(1)}-01"
        return ym, _month_label_fr(ym)
    fallback = export_now.strftime("%Y-%m")
    return fallback, _month_label_fr(fallback)


def _maybe_write_geojson(path: Path, payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        return False
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _has_features(payload: dict[str, Any] | None) -> bool:
    return bool(payload and isinstance(payload.get("features"), list) and payload.get("features"))


def _ogr_field_type(value: Any) -> int:
    if isinstance(value, bool):
        return ogr.OFTInteger
    if isinstance(value, int) and not isinstance(value, bool):
        return ogr.OFTInteger64
    if isinstance(value, float):
        return ogr.OFTReal
    return ogr.OFTString


def _coerce_ogr_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bool):
        return int(value)
    return json.dumps(value, ensure_ascii=False)


def _write_feature_collection_to_gpkg(
    dataset: ogr.DataSource,
    *,
    layer_name: str,
    feature_collection: dict[str, Any],
) -> bool:
    if not _has_features(feature_collection):
        return False

    features = feature_collection["features"]
    geometries = [feature.get("geometry") for feature in features if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)]
    if not geometries:
        return False

    geometry_types: set[str] = set()
    field_types: dict[str, int] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if isinstance(geometry, dict) and isinstance(geometry.get("type"), str):
            geometry_types.add(geometry["type"])
        properties = feature.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                if not isinstance(key, str):
                    continue
                inferred = _ogr_field_type(value)
                existing = field_types.get(key)
                if existing is None:
                    field_types[key] = inferred
                elif existing != inferred:
                    field_types[key] = ogr.OFTString

    geometry_type = ogr.wkbUnknown
    if geometry_types and geometry_types.issubset({"Polygon", "MultiPolygon"}):
        geometry_type = ogr.wkbMultiPolygon
    elif geometry_types and geometry_types.issubset({"LineString", "MultiLineString"}):
        geometry_type = ogr.wkbMultiLineString
    elif geometry_types and geometry_types.issubset({"Point", "MultiPoint"}):
        geometry_type = ogr.wkbMultiPoint

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = dataset.CreateLayer(layer_name, srs=srs, geom_type=geometry_type)
    if layer is None:
        raise ValueError(f"Unable to create GeoPackage layer: {layer_name}")

    ordered_field_names = sorted(field_types)
    for field_name in ordered_field_names:
        field_defn = ogr.FieldDefn(field_name, field_types[field_name])
        layer.CreateField(field_defn)

    layer_defn = layer.GetLayerDefn()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            continue
        ogr_geometry = ogr.CreateGeometryFromJson(json.dumps(geometry_payload))
        if ogr_geometry is None:
            continue
        if geometry_type == ogr.wkbMultiPolygon and ogr_geometry.GetGeometryType() == ogr.wkbPolygon:
            ogr_geometry = ogr.ForceToMultiPolygon(ogr_geometry)
        elif geometry_type == ogr.wkbMultiLineString and ogr_geometry.GetGeometryType() == ogr.wkbLineString:
            ogr_geometry = ogr.ForceToMultiLineString(ogr_geometry)
        elif geometry_type == ogr.wkbMultiPoint and ogr_geometry.GetGeometryType() == ogr.wkbPoint:
            ogr_geometry = ogr.ForceToMultiPoint(ogr_geometry)
        record = ogr.Feature(layer_defn)
        record.SetGeometry(ogr_geometry)
        properties = feature.get("properties")
        if isinstance(properties, dict):
            for field_name in ordered_field_names:
                if field_name not in properties:
                    continue
                value = _coerce_ogr_value(properties.get(field_name))
                if value is not None:
                    record.SetField(field_name, value)
        if feature.get("id") is not None and layer_defn.GetFieldIndex("feature_id") >= 0:
            record.SetField("feature_id", str(feature.get("id")))
        if layer.CreateFeature(record) != 0:
            raise ValueError(f"Unable to append feature to GeoPackage layer: {layer_name}")
        record = None
        ogr_geometry = None
    layer.SyncToDisk()
    dataset.FlushCache()
    layer = None
    return True


def _validate_raster_for_qgis_export(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"Raster has no CRS: {path}")
        if src.transform is None:
            raise ValueError(f"Raster has no transform: {path}")
        if src.width <= 0 or src.height <= 0:
            raise ValueError(f"Raster has invalid dimensions: {path}")
        bounds = src.bounds
        if bounds is None:
            raise ValueError(f"Raster has no bounds: {path}")
        overview_count = sum(1 for idx in range(1, src.count + 1) if src.overviews(idx))
        if overview_count == 0:
            logger.warning("QGIS_EXPORT_RASTER_NO_OVERVIEWS path=%s", path)
        return {
            "path": str(path),
            "crs": str(src.crs),
            "bounds": [float(bounds.left), float(bounds.bottom), float(bounds.right), float(bounds.top)],
            "overview_count": overview_count,
        }


def _write_qgis_project_xml(
    *,
    project_name: str,
    layer_groups: dict[str, list[dict[str, str]]],
) -> str:
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<qgis projectname="" version="3.34.0">',
        f"  <title>{project_name}</title>",
        '  <layer-tree-group name="Projet temporel" expanded="1">',
    ]

    for group_name, layers in layer_groups.items():
        lines.append(f'    <layer-tree-group name="{group_name}" expanded="1">')
        for layer in layers:
            lines.append(
                f'      <layer-tree-layer id="{layer["id"]}" name="{layer["name"]}" checked="Qt::Checked" expanded="0"/>'
            )
        lines.append("    </layer-tree-group>")

    lines.extend(
        [
            "  </layer-tree-group>",
            "  <projectlayers>",
        ]
    )

    for layers in layer_groups.values():
        for layer in layers:
            provider = "gdal" if layer["type"] == "raster" else "ogr"
            lines.extend(
                [
                    f'    <maplayer type="{layer["type"]}" name="{layer["name"]}" id="{layer["id"]}">',
                    f'      <datasource>{layer["path"]}</datasource>',
                    f'      <provider>{provider}</provider>',
                    "    </maplayer>",
                ]
            )

    lines.extend(["  </projectlayers>", "</qgis>"])
    return "\n".join(lines)


def _write_qgz_project(
    path: Path,
    *,
    project_name: str,
    layer_groups: dict[str, list[dict[str, str]]],
) -> str:
    xml_text = _write_qgis_project_xml(project_name=project_name, layer_groups=layer_groups)
    internal_qgs_name = f"{path.stem}.qgs"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr(internal_qgs_name, xml_text)
    return internal_qgs_name


def _load_qgs_from_qgz(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path, "r") as archive:
        qgs_members = [name for name in archive.namelist() if name.lower().endswith(".qgs")]
        if len(qgs_members) != 1:
            raise ValueError(f"Expected exactly one .qgs inside {path}, found {qgs_members}")
        member = qgs_members[0]
        return member, archive.read(member).decode("utf-8")


def _list_gpkg_layers(path: Path) -> tuple[set[str], str]:
    try:
        import pyogrio

        return {str(row[0]) for row in pyogrio.list_layers(path)}, "pyogrio"
    except Exception:
        pass
    try:
        import fiona

        return set(fiona.listlayers(path)), "fiona"
    except Exception:
        pass
    datasource = ogr.Open(str(path), 0)
    if datasource is None:
        raise ValueError(f"Unable to open GeoPackage with pyogrio, fiona, or ogr: {path}")
    layers = {datasource.GetLayerByIndex(index).GetName() for index in range(datasource.GetLayerCount())}
    datasource = None
    return layers, "ogr"


def _validate_temporal_qgis_export(
    *,
    export_build_dir: Path,
    qgz_path: Path,
    gpkg_path: Path,
    expected_gpkg_layers: set[str],
    raster_paths: list[Path],
    attempted_layer_count: int,
) -> None:
    if not qgz_path.exists():
        raise ValueError("QGIS export missing .qgz project.")
    gpkg_exists = gpkg_path.exists()
    gpkg_size = gpkg_path.stat().st_size if gpkg_exists else 0
    logger.info(
        "QGIS_GPKG_VALIDATE_INPUT path=%s exists=%s size=%s attempted_layer_count=%s written_layer_count=%s validation_backend=pyogrio|fiona|ogr",
        gpkg_path,
        gpkg_exists,
        gpkg_size,
        attempted_layer_count,
        len(expected_gpkg_layers),
    )
    if not gpkg_exists or gpkg_size <= 0 or not expected_gpkg_layers:
        raise ValueError(
            "Invalid QGIS GeoPackage before validation: "
            f"exists={gpkg_exists} size={gpkg_size} attempted_layers={attempted_layer_count} "
            f"written_layers={len(expected_gpkg_layers)} validation_backend=pyogrio|fiona|ogr"
        )

    forbidden_suffixes = {".csv", ".geojson", ".zip"}
    forbidden_names = {"LISEZ_MOI.txt"}
    for path in export_build_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name in forbidden_names:
            raise ValueError(f"Forbidden file exported: {path.name}")
        if path.suffix.lower() in forbidden_suffixes and path != qgz_path:
            raise ValueError(f"Forbidden artifact exported: {path}")

    internal_qgs_name, qgs_text = _load_qgs_from_qgz(qgz_path)
    if "/Users/" in qgs_text or "C:\\" in qgs_text or "../.." in qgs_text:
        raise ValueError("QGIS project contains absolute or invalid upward paths.")
    forbidden_refs = [
        ".csv",
        ".geojson",
        "LISEZ_MOI",
        "change_probability",
        "building_change_labels",
        "manual_override",
        "reference_labels",
        "automated_candidate_footprint",
        "effective_footprint",
        "cumulative_growth_blocks",
        "cumulative_growth_envelope",
    ]
    if any(token in qgs_text for token in forbidden_refs):
        raise ValueError("QGIS project references forbidden artifacts.")

    root = ET.fromstring(qgs_text)
    datasources = [element.text or "" for element in root.findall(".//datasource")]
    project_dir = qgz_path.parent
    for datasource in datasources:
        if datasource.startswith("../donnees/vecteurs/"):
            rel_path, _, layer_part = datasource.partition("|layername=")
            target = (project_dir / rel_path).resolve()
            if not target.exists():
                raise ValueError(f"Missing GPKG datasource referenced by project: {datasource}")
            layer_name = layer_part.strip()
            if layer_name not in expected_gpkg_layers:
                raise ValueError(f"Referenced GPKG layer missing from expected export set: {layer_name}")
        elif datasource.startswith("../donnees/rasters/"):
            target = (project_dir / datasource).resolve()
            if not target.exists():
                raise ValueError(f"Missing raster datasource referenced by project: {datasource}")
        else:
            raise ValueError(f"Unexpected datasource path in QGIS project: {datasource}")

    try:
        actual_layers, validation_backend = _list_gpkg_layers(gpkg_path)
    except Exception as exc:
        raise ValueError(
            "Unable to open exported GeoPackage: "
            f"path={gpkg_path} exists={gpkg_exists} size={gpkg_size} "
            f"attempted_layers={attempted_layer_count} written_layers={len(expected_gpkg_layers)} "
            f"validation_backend=pyogrio|fiona|ogr error={exc}"
        ) from exc
    logger.info(
        "QGIS_GPKG_VALIDATE_DONE path=%s validation_backend=%s actual_layer_count=%s",
        gpkg_path,
        validation_backend,
        len(actual_layers),
    )
    missing_layers = expected_gpkg_layers - actual_layers
    if missing_layers:
        raise ValueError(f"GeoPackage missing expected layers: {sorted(missing_layers)}")

    for raster_path in raster_paths:
        _validate_raster_for_qgis_export(raster_path)


def _write_qgis_project(
    path: Path,
    *,
    project_name: str,
    layer_groups: dict[str, list[dict[str, str]]],
) -> None:
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<qgis projectname="" version="3.34.0">',
        f'  <title>{project_name}</title>',
        '  <layer-tree-group name="Projet temporel" expanded="1">',
    ]

    for group_name, layers in layer_groups.items():
        lines.append(f'    <layer-tree-group name="{group_name}" expanded="1">')
        for layer in layers:
            lines.append(
                f'      <layer-tree-layer id="{layer["id"]}" name="{layer["name"]}" checked="Qt::Checked" expanded="0"/>'
            )
        lines.append("    </layer-tree-group>")

    lines.extend(
        [
            "  </layer-tree-group>",
            "  <projectlayers>",
        ]
    )

    for layers in layer_groups.values():
        for layer in layers:
            provider = "gdal" if layer["type"] == "raster" else "ogr"
            lines.extend(
                [
                    f'    <maplayer type="{layer["type"]}" name="{layer["name"]}" id="{layer["id"]}">',
                    f'      <datasource>{layer["path"]}</datasource>',
                    f'      <provider>{provider}</provider>',
                    "    </maplayer>",
                ]
            )

    lines.extend(["  </projectlayers>", "</qgis>"])
    path.write_text("\n".join(lines), encoding="utf-8")


def create_temporal_project_bundle(project_id: str, *, settings: Settings, force: bool = False) -> Path:
    project = _load_project(
        settings,
        project_id,
        hydrate_reference_imagery=True,
        hydrate_buffer_layers=True,
        refresh_derived_layers=True,
        write_side_effects=False,
    )
    project = _hydrate_temporal_layer_artifacts(project, settings)
    project = _ensure_temporal_derived_geometry_layers(project, settings)
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    export_now = datetime.now(UTC)
    milestone_dates: list[str] = []
    milestone_context: list[tuple[TemporalMilestone, str, str, Path | None]] = []
    for milestone in project.milestones:
        pair_dir = settings.request_cache_dir / milestone.pair_request_hash if milestone.pair_request_hash else None
        year_month, label = _milestone_source_year_month(milestone, pair_dir=pair_dir, export_now=export_now)
        milestone_dates.append(year_month)
        milestone_context.append((milestone, year_month, label, pair_dir))

    start_ym = min(milestone_dates) if milestone_dates else export_now.strftime("%Y-%m")
    end_ym = max(milestone_dates) if milestone_dates else export_now.strftime("%Y-%m")
    safe_name = _safe_export_name(project.name or project.project_id)
    bundle_name = f"{safe_name}_{start_ym}_{end_ym}_export_QGIS.zip"
    bundle_path = project_dir / bundle_name
    if bundle_path.exists() and not force:
        return bundle_path

    export_root = f"{safe_name}_{start_ym}_{end_ym}_export_QGIS"
    export_build_dir = project_dir / export_root
    if export_build_dir.exists():
        shutil.rmtree(export_build_dir, ignore_errors=True)

    qgis_dir = export_build_dir / "qgis"
    data_dir = export_build_dir / "donnees"
    rasters_dir = data_dir / "rasters"
    vectors_dir = data_dir / "vecteurs"
    styles_dir = export_build_dir / "styles"
    for path in (qgis_dir, rasters_dir, vectors_dir, styles_dir):
        path.mkdir(parents=True, exist_ok=True)

    gpkg_name = f"{safe_name}_{start_ym}_{end_ym}.gpkg"
    gpkg_path = vectors_dir / gpkg_name
    gpkg_driver = ogr.GetDriverByName("GPKG")
    if gpkg_driver is None:
        raise ValueError("GDAL GeoPackage driver is unavailable.")
    if gpkg_path.exists():
        gpkg_driver.DeleteDataSource(str(gpkg_path))
    gpkg_dataset = gpkg_driver.CreateDataSource(str(gpkg_path))
    if gpkg_dataset is None:
        raise ValueError(f"Unable to create GeoPackage export: {gpkg_path}")

    qgis_groups: dict[str, list[dict[str, str]]] = {}
    gpkg_layer_names: list[str] = []
    raster_files: list[str] = []
    raster_output_paths: list[Path] = []
    skipped_layers: list[dict[str, str]] = []
    attempted_gpkg_layer_count = 0

    def register_layer(group: str, name: str, rel_path: str, layer_type: str) -> None:
        layer_id = f"{re.sub(r'[^a-zA-Z0-9_]', '_', name)}_{uuid.uuid4().hex[:8]}"
        qgis_groups.setdefault(group, []).append(
            {
                "id": layer_id,
                "name": name,
                "path": rel_path,
                "type": layer_type,
            }
        )

    vector_specs = [
        ("additions_geojson", "ajouts", "Ajouts"),
        ("buffer_layers_geojson.10m", "tampon_changement_batiment_10m", "Tampon changement bâtiment 10 m"),
        ("buffer_layers_geojson.15m", "tampon_changement_batiment_15m", "Tampon changement bâtiment 15 m"),
        ("buffer_layers_geojson.20m", "tampon_changement_batiment_20m", "Tampon changement bâtiment 20 m"),
        ("cumulative_buffer_10m", "tampon_cumulatif_changement_batiment_10m", "Tampon cumulatif changement bâtiment 10 m"),
        ("cumulative_buffer_15m", "tampon_cumulatif_changement_batiment_15m", "Tampon cumulatif changement bâtiment 15 m"),
        ("cumulative_buffer_20m", "tampon_cumulatif_changement_batiment_20m", "Tampon cumulatif changement bâtiment 20 m"),
    ]

    try:
        for milestone_index, (milestone, year_month, label, pair_dir) in enumerate(milestone_context):
            date_prefix = year_month.replace("-", "_")
            milestone_slug = milestone.release_identifier
            group_name = label

            reference = milestone.reference_imagery
            raster_source_path: str | None = None
            if reference is not None:
                raster_source_path = reference.cog_path or reference.image_path
            if (not raster_source_path) and pair_dir is not None:
                fallback = pair_dir / "t2_wayback_rgb.tif"
                if fallback.exists():
                    raster_source_path = str(fallback)
            if raster_source_path:
                source = Path(raster_source_path)
                if source.exists() and source.suffix.lower() in {".tif", ".tiff"}:
                    raster_name = f"{date_prefix}_{milestone_slug}_imagerie_de_reference.tif"
                    dst = rasters_dir / raster_name
                    shutil.copy2(source, dst)
                    _validate_raster_for_qgis_export(dst)
                    raster_files.append(raster_name)
                    raster_output_paths.append(dst)
                    register_layer(group_name, f"Imagerie de référence - {label}", f"../donnees/rasters/{raster_name}", "raster")
                else:
                    skipped_layers.append({"layer": f"reference_imagery:{milestone.release_identifier}", "reason": "missing_or_non_tiff_raster"})
            else:
                skipped_layers.append({"layer": f"reference_imagery:{milestone.release_identifier}", "reason": "no_reference_raster"})

            for key, layer_stub, layer_label_prefix in vector_specs:
                attempted_gpkg_layer_count += 1
                payload: dict[str, Any] | None = None
                if key.startswith("buffer_layers_geojson."):
                    distance_key = key.split(".", 1)[1]
                    payload = milestone.buffer_layers_geojson.get(distance_key)
                elif key.startswith("cumulative_buffer_"):
                    distance = key.replace("cumulative_buffer_", "").replace("m", "")
                    cumulative_layers = [
                        (ctx_milestone.buffer_layers_geojson.get(f"{distance}m") or ctx_milestone.buffer_layers_geojson.get(distance))
                        for ctx_milestone, _, _, _ in milestone_context[: milestone_index + 1]
                    ]
                    features: list[dict[str, Any]] = []
                    for layer in cumulative_layers:
                        if isinstance(layer, dict):
                            feats = layer.get("features")
                            if isinstance(feats, list):
                                features.extend(feats)
                    payload = {"type": "FeatureCollection", "features": features}
                else:
                    payload = getattr(milestone, key, None)

                if not _has_features(payload):
                    skipped_layers.append({"layer": f"{layer_stub}:{milestone.release_identifier}", "reason": "empty_or_missing"})
                    logger.info(
                        "QGIS_GPKG_LAYER_SKIPPED_EMPTY layer=%s source=%s feature_count=0",
                        f"{layer_stub}_{date_prefix}",
                        f"{milestone.release_identifier}:{key}",
                    )
                    continue

                gpkg_layer_name = f"{layer_stub}_{date_prefix}"
                logger.info(
                    "QGIS_GPKG_LAYER_WRITE layer=%s source=%s feature_count=%s",
                    gpkg_layer_name,
                    f"{milestone.release_identifier}:{key}",
                    len(payload.get("features", [])),
                )
                if _write_feature_collection_to_gpkg(
                    gpkg_dataset,
                    layer_name=gpkg_layer_name,
                    feature_collection=payload or _empty_feature_collection(),
                ):
                    gpkg_layer_names.append(gpkg_layer_name)
                    register_layer(
                        group_name,
                        f"{layer_label_prefix} - {label}",
                        f"../donnees/vecteurs/{gpkg_name}|layername={gpkg_layer_name}",
                        "vector",
                    )
                else:
                    skipped_layers.append({"layer": f"{layer_stub}:{milestone.release_identifier}", "reason": "failed_to_write_gpkg_layer"})
    finally:
        if gpkg_dataset is not None:
            gpkg_dataset.FlushCache()
        gpkg_dataset = None

    manifest_path = export_build_dir / "manifeste_projet.json"
    qgz_name = f"{safe_name}_{start_ym}_{end_ym}.qgz"
    qgz_path = qgis_dir / qgz_name
    internal_qgs_name = _write_qgz_project(
        qgz_path,
        project_name=project.name,
        layer_groups=qgis_groups,
    )

    try:
        _validate_temporal_qgis_export(
            export_build_dir=export_build_dir,
            qgz_path=qgz_path,
            gpkg_path=gpkg_path,
            expected_gpkg_layers=set(gpkg_layer_names),
            raster_paths=raster_output_paths,
            attempted_layer_count=attempted_gpkg_layer_count,
        )
    except Exception:
        shutil.rmtree(export_build_dir, ignore_errors=True)
        raise

    manifest_path.write_text(
        json.dumps(
            {
                "project_id": project.project_id,
                "project_name": project.name,
                "export_filename": bundle_name,
                "date_range": {"start": start_ym, "end": end_ym},
                "qgz_path": f"qgis/{qgz_name}",
                "qgs_internal_name": internal_qgs_name,
                "gpkg_path": f"donnees/vecteurs/{gpkg_name}",
                "raster_files": raster_files,
                "gpkg_layer_names": gpkg_layer_names,
                "skipped_layers": skipped_layers,
                "notes": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(export_build_dir.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"{export_root}/{path.relative_to(export_build_dir)}")

    project.download_bundle_path = str(bundle_path)
    _save_project(project, settings)
    return bundle_path


def _load_project(
    settings: Settings,
    project_id: str,
    *,
    hydrate_reference_imagery: bool = False,
    hydrate_buffer_layers: bool = True,
    refresh_derived_layers: bool = True,
    write_side_effects: bool = True,
) -> TemporalProject:
    load_started_at = time.perf_counter()
    path = _project_json_path(settings, project_id)
    if not path.exists():
        raise FileNotFoundError(f"Unknown temporal project: {project_id}")
    metadata_started_at = time.perf_counter()
    payload = json.loads(path.read_text())
    stripped_fields = [
        field
        for field in ("has_reference_layers", "reference_layer_count")
        if isinstance(payload, dict) and field in payload
    ]
    if stripped_fields:
        for field in stripped_fields:
            payload.pop(field, None)
        logger.info(
            "TEMPORAL_PROJECT_STRIPPED_DERIVED_FIELDS projectId=%s fields=%s",
            project_id,
            ",".join(stripped_fields),
        )
    project = validate_stored_temporal_project(payload)
    project.project_dir = str(path.parent.resolve())
    for milestone in project.milestones:
        milestone.artifacts = _allowed_temporal_artifacts(milestone.artifacts)
    project.execution_config = resolve_temporal_project_execution_config(project, settings)
    project = _populate_milestone_release_dates(project, settings)
    if project.project_dir is None:
        project.project_dir = str(path.parent)
    project = _sort_temporal_milestones(project)
    logger.info(
        "PROJECT_LOAD_TIMING projectId=%s phase=metadata ms=%s",
        project_id,
        round((time.perf_counter() - metadata_started_at) * 1000, 2),
    )
    initial_project_payload = json.dumps(project.model_dump(mode="json"), sort_keys=True) if write_side_effects else None
    should_compact_project_json = _strip_redundant_reference_imagery_data_urls(project) if write_side_effects else False
    layer_availability_started_at = time.perf_counter()
    if hydrate_reference_imagery:
        project = _hydrate_reference_imagery(project, settings)
    if hydrate_buffer_layers:
        project = _hydrate_milestone_buffer_layers(project, settings)
    if refresh_derived_layers:
        project = _hydrate_temporal_layer_artifacts(project, settings)
        project = _ensure_temporal_derived_geometry_layers(project, settings)
    logger.info(
        "PROJECT_LOAD_TIMING projectId=%s phase=layer_availability ms=%s",
        project_id,
        round((time.perf_counter() - layer_availability_started_at) * 1000, 2),
    )
    metadata_externalized = False
    stripped_large_payloads = 0
    if write_side_effects:
        project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
        project.project_dir = str(project_dir)
        externalized_count, empty_baseline_artifacts_removed = _externalize_temporal_artifact_payloads_in_project(
            project=project,
            project_dir=project_dir,
        )
        stripped_large_payloads = _strip_large_inline_temporal_result_payloads(project, settings)
        metadata_externalized = bool(externalized_count or empty_baseline_artifacts_removed)
        if metadata_externalized:
            logger.info(
                "TEMPORAL_PROJECT_METADATA_PAYLOAD_EXTERNALIZED projectId=%s source=load_project externalizedArtifacts=%s emptyBaselineArtifactsRemoved=%s",
                project.project_id,
                externalized_count,
                empty_baseline_artifacts_removed,
            )
    final_project_payload = json.dumps(project.model_dump(mode="json"), sort_keys=True) if write_side_effects else None
    if write_side_effects and (
        metadata_externalized
        or stripped_large_payloads
        or should_compact_project_json
        or stripped_fields
        or final_project_payload != initial_project_payload
    ):
        path.write_text(json.dumps(project.model_dump(mode="json"), indent=2))
        manifest_path = path.with_name("project_manifest.json")
        if manifest_path.exists():
            manifest_path.write_text(json.dumps(project.model_dump(mode="json"), indent=2))
    if write_side_effects:
        try:
            _write_project_summary(project, path)
        except Exception:
            pass
    logger.info(
        "PROJECT_LOAD_TIMING projectId=%s phase=total ms=%s",
        project_id,
        round((time.perf_counter() - load_started_at) * 1000, 2),
    )
    return project


def _save_project(project: TemporalProject, settings: Settings) -> TemporalProject:
    project = _populate_milestone_release_dates(project, settings)
    project = _sort_temporal_milestones(project)
    project.execution_config = resolve_temporal_project_execution_config(project, settings)
    project = _hydrate_reference_imagery(project, settings)
    project = _hydrate_temporal_layer_artifacts(project, settings)
    project = _hydrate_milestone_buffer_layers(project, settings)
    project = _refresh_temporal_derived_geometry_layers(project, settings)
    _strip_redundant_reference_imagery_data_urls(project)
    _strip_large_inline_temporal_result_payloads(project, settings)
    project.updated_at = _utc_now_iso()
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    project.project_dir = str(project_dir)
    reference_materialized_count = _ensure_temporal_project_reference_imagery_from_canonical_cache(
        project=project,
        settings=settings,
        project_dir=project_dir,
    )
    if reference_materialized_count:
        logger.info(
            "TEMPORAL_REFERENCE_FINALIZATION_DONE projectId=%s materializedCount=%s",
            project.project_id,
            reference_materialized_count,
        )
    externalized_count, empty_baseline_artifacts_removed = _externalize_temporal_artifact_payloads_in_project(
        project=project,
        project_dir=project_dir,
    )
    if externalized_count or empty_baseline_artifacts_removed:
        logger.info(
            "TEMPORAL_PROJECT_METADATA_PAYLOAD_EXTERNALIZED projectId=%s source=save_project externalizedArtifacts=%s emptyBaselineArtifactsRemoved=%s",
            project.project_id,
            externalized_count,
            empty_baseline_artifacts_removed,
        )
    registry = _load_project_registry(settings)
    registry[project.project_id] = str(project_dir)
    _save_project_registry(settings, registry)
    path = project_dir / "project.json"
    path.write_text(json.dumps(project.model_dump(mode="json"), indent=2))
    (project_dir / "project_manifest.json").write_text(json.dumps(project.model_dump(mode="json"), indent=2))
    _write_project_summary(project, path)
    logger.info(
        "TEMPORAL_PROJECT_PUBLICATION_SUMMARY_UPDATED projectId=%s path=%s",
        project.project_id,
        _project_summary_json_path(path),
    )
    return project


def _pair_summary_rows(result_dir: Path) -> dict[str, dict[str, str]] | None:
    path = result_dir / "wayback_pair_summary.csv"
    if not path.exists():
        return None

    try:
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return None

    summary: dict[str, dict[str, str]] = {}
    for row in rows:
        label = (row.get("label") or "").strip().lower()
        if label in {"t1", "t2"}:
            summary[label] = row
    return summary or None


def _load_cached_run_response(settings: Settings, request_hash: str) -> RunResponse | None:
    try:
        if _cached_response_has_stale_fallback_imagery(settings, request_hash):
            return None
        return load_cached_response(settings, request_hash)
    except Exception:
        return None


def _run_project_id(request_hash: str) -> str:
    return f"run-{_safe_project_id(request_hash)}"


def _cached_run_directory(settings: Settings, request_hash: str) -> Path:
    return settings.request_cache_dir / request_hash


def _normalize_baseline_milestone(milestone: TemporalMilestone) -> None:
    milestone.pair_request_hash = None
    if milestone.status != "error":
        milestone.error_message = None


def _prepare_temporal_pair_request(
    *,
    aoi_geojson: dict[str, Any],
    previous_release_identifier: str,
    milestone_release_identifier: str,
    releases,
    settings: Settings,
    remote_patch_budget_enabled: bool,
    request_hash_context: dict[str, object] | None,
    existing_footprint_geojson: dict[str, Any] | None = None,
):
    validation_request_kwargs: dict[str, Any] = {
        "aoi_geojson": aoi_geojson,
        "t1_release": previous_release_identifier,
        "t2_release": milestone_release_identifier,
        "mode": "full_run",
        "existing_footprint_geojson": existing_footprint_geojson,
    }
    if request_hash_context and request_hash_context.get("threshold_source") == "request_override":
        validation_request_kwargs["change_threshold"] = float(request_hash_context["change_threshold"])
    validation_request = ValidationRequest(**validation_request_kwargs)
    validation_response, prepared = validate_request(
        validation_request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=remote_patch_budget_enabled,
        request_hash_context=request_hash_context,
    )
    return validation_request, validation_response, prepared


def _build_temporal_imagery_prefetch_plan(
    project: TemporalProject,
    pair_plan: list[TemporalMilestonePlanEntry],
    *,
    settings: Settings,
) -> list[TemporalImageryPrefetchPlan]:
    if project.aoi_geojson is None:
        return []

    releases = list_releases(settings)
    prefetch_plans: list[TemporalImageryPrefetchPlan] = []
    for entry in pair_plan:
        if entry.index == 0 or entry.reusable or entry.blocking_errors or entry.expected_request_hash is None:
            continue
        milestone = project.milestones[entry.index]
        prefetch_plans.append(
            TemporalImageryPrefetchPlan(
                pair_index=entry.index,
                request_hash=entry.expected_request_hash,
                t1_provider="esri_wayback",
                t2_provider="esri_wayback",
                t1_release_identifier=entry.previous_release_identifier or milestone.release_identifier,
                t2_release_identifier=milestone.release_identifier,
                aoi_geojson=project.aoi_geojson,
                t2_effective_release_identifier=milestone.release_identifier,
            )
        )
        if len(prefetch_plans) >= settings.temporal_imagery_prefetch_max_pairs:
            break
    return prefetch_plans


def _prefetch_provider_worker_settings(settings: Settings) -> Settings:
    provider_workers = settings.download_workers
    if settings.temporal_imagery_prefetch_reduce_provider_workers:
        provider_workers = max(1, settings.download_workers // settings.temporal_imagery_prefetch_workers)
    return settings.model_copy(
        update={
            "download_workers": provider_workers,
            "materialize_source_imagery_in_requests": False,
        }
    )


def _prefetch_pair_imagery(
    plan: TemporalImageryPrefetchPlan,
    *,
    settings: Settings,
    releases_by_id: dict[str, Any],
) -> TemporalImageryPrefetchResult:
    started_ns = time.perf_counter_ns()
    derived_settings = _prefetch_provider_worker_settings(settings)
    geometry = parse_aoi_geometry(plan.aoi_geojson)
    aoi_bbox = bounds_dict(geometry)
    metadata: dict[str, Any] = {
        "pair_index": plan.pair_index,
        "request_hash": plan.request_hash,
        "t1_provider": plan.t1_provider,
        "t2_provider": plan.t2_provider,
        "provider_download_workers": derived_settings.download_workers,
    }
    temp_dir_path = Path(
        tempfile.mkdtemp(
            prefix=f"temporal-prefetch-{plan.request_hash}-",
            dir=str(settings.tmp_cache_dir),
        )
    )
    try:
        t1_release = releases_by_id[plan.t1_release_identifier]
        resolved_t1 = _resolve_release_for_aoi(
            derived_settings,
            release=t1_release,
            aoi_bbox=aoi_bbox,
            normalized_aoi=plan.aoi_geojson,
            scene_role="prefetch_t1",
            stage_prefix="temporal_prefetch.t1",
        )
        scene_t1 = EsriWaybackProvider().download(
            t1_release,
            aoi_bbox,
            settings=derived_settings,
            zoom=resolved_t1.zoom,
            out_dir=temp_dir_path,
            label="prefetch_t1",
            available_tiles=resolved_t1.tilemap.available_tiles if resolved_t1.tilemap is not None and resolved_t1.tilemap.preflight_complete else None,
        )
        metadata["t1_cache_key"] = scene_t1.cache_key
        metadata["t1_zoom"] = resolved_t1.zoom

        t2_release = releases_by_id[plan.t2_effective_release_identifier]
        resolved_t2 = _resolve_release_for_aoi(
            derived_settings,
            release=t2_release,
            aoi_bbox=aoi_bbox,
            normalized_aoi=plan.aoi_geojson,
            scene_role="prefetch_t2",
            stage_prefix="temporal_prefetch.t2",
        )
        scene_t2 = EsriWaybackProvider().download(
            t2_release,
            aoi_bbox,
            settings=derived_settings,
            zoom=resolved_t2.zoom,
            out_dir=temp_dir_path,
            label="prefetch_t2",
            available_tiles=resolved_t2.tilemap.available_tiles if resolved_t2.tilemap is not None and resolved_t2.tilemap.preflight_complete else None,
        )
        metadata["t2_cache_key"] = scene_t2.cache_key
        metadata["t2_zoom"] = resolved_t2.zoom

        return TemporalImageryPrefetchResult(
            pair_index=plan.pair_index,
            request_hash=plan.request_hash,
            t1_provider=plan.t1_provider,
            t2_provider=plan.t2_provider,
            status="success",
            cache_hit_or_warmed=True,
            duration_ms=round((time.perf_counter_ns() - started_ns) / 1_000_000, 2),
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        metadata["exception_class"] = type(exc).__name__
        return TemporalImageryPrefetchResult(
            pair_index=plan.pair_index,
            request_hash=plan.request_hash,
            t1_provider=plan.t1_provider,
            t2_provider=plan.t2_provider,
            status="failed",
            cache_hit_or_warmed=False,
            duration_ms=round((time.perf_counter_ns() - started_ns) / 1_000_000, 2),
            metadata=metadata,
            warning=str(exc),
        )
    finally:
        shutil.rmtree(temp_dir_path, ignore_errors=True)


def _run_temporal_imagery_prefetch(
    project: TemporalProject,
    *,
    settings: Settings,
    pair_plan: list[TemporalMilestonePlanEntry],
    timing: StageTimingRecorder | None = None,
) -> list[TemporalImageryPrefetchResult]:
    if not settings.temporal_imagery_prefetch_enabled:
        return []

    prefetch_plans = _build_temporal_imagery_prefetch_plan(project, pair_plan, settings=settings)
    if not prefetch_plans:
        return []

    releases_by_id = {release.identifier: release for release in list_releases(settings)}
    results: list[TemporalImageryPrefetchResult] = []
    worker_count = min(settings.temporal_imagery_prefetch_workers, len(prefetch_plans))
    total_started_ns = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_prefetch_pair_imagery, plan, settings=settings, releases_by_id=releases_by_id): plan
            for plan in prefetch_plans
        }
        try:
            for future in as_completed(future_map, timeout=settings.temporal_imagery_prefetch_timeout_seconds):
                result = future.result()
                results.append(result)
                if timing is not None:
                    timing.add_stage(
                        f"temporal_imagery_prefetch.pair_{result.pair_index}",
                        duration_ms=result.duration_ms,
                        status="failed" if result.status == "failed" else "success",
                        metadata={
                            "pair_index": result.pair_index,
                            "request_hash": result.request_hash,
                            "t1_provider": result.t1_provider,
                            "t2_provider": result.t2_provider,
                            "status": result.status,
                            "cache_hit_or_warmed": result.cache_hit_or_warmed,
                            **result.metadata,
                        },
                        error_type=result.metadata.get("exception_class") if result.status == "failed" else None,
                    )
        except FuturesTimeoutError:
            for future, plan in future_map.items():
                if future.done():
                    continue
                future.cancel()
                timeout_result = TemporalImageryPrefetchResult(
                    pair_index=plan.pair_index,
                    request_hash=plan.request_hash,
                    t1_provider=plan.t1_provider,
                    t2_provider=plan.t2_provider,
                    status="failed",
                    cache_hit_or_warmed=False,
                    duration_ms=round((time.perf_counter_ns() - total_started_ns) / 1_000_000, 2),
                    metadata={"exception_class": "TimeoutError"},
                    warning="Temporal imagery prefetch timed out.",
                )
                results.append(timeout_result)
                if timing is not None:
                    timing.add_stage(
                        f"temporal_imagery_prefetch.pair_{plan.pair_index}",
                        duration_ms=timeout_result.duration_ms,
                        status="failed",
                        metadata={
                            "pair_index": plan.pair_index,
                            "request_hash": plan.request_hash,
                            "t1_provider": plan.t1_provider,
                            "t2_provider": plan.t2_provider,
                            "status": "failed",
                            "cache_hit_or_warmed": False,
                        },
                        error_type="TimeoutError",
                    )
        finally:
            if timing is not None:
                timing.add_stage(
                    "temporal_imagery_prefetch_total",
                    duration_ms=round((time.perf_counter_ns() - total_started_ns) / 1_000_000, 2),
                    metadata={
                        "pair_count": len(prefetch_plans),
                        "worker_count": worker_count,
                        "success_count": sum(1 for item in results if item.status == "success"),
                        "failure_count": sum(1 for item in results if item.status == "failed"),
                    },
                )
    return sorted(results, key=lambda item: item.pair_index)


def _write_temporal_project_timing_safely(timing: StageTimingRecorder, project: TemporalProject) -> None:
    project_dir = _normalize_project_dir(project.project_dir)
    if project_dir is None:
        return
    try:
        timing.write_timing_report(project_dir / "timing.json")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write temporal project timing report for %s: %s", project.project_id, exc)


def _plan_temporal_milestone_runs(
    project: TemporalProject,
    *,
    settings: Settings,
    remote_patch_budget_enabled: bool,
    request_hash_context: dict[str, object] | None,
) -> list[TemporalMilestonePlanEntry]:
    if project.aoi_geojson is None:
        return []

    releases = list_releases(settings)
    latest_wayback_release = max(releases, key=lambda item: item.release_date) if releases else None
    plan: list[TemporalMilestonePlanEntry] = []
    previous_release_id: str | None = None
    previous_successful_release_id: str | None = None
    previous_cumulative = GeometryCollection()

    last_index = len(project.milestones) - 1
    for index, milestone in enumerate(project.milestones):
        if index == 0:
            _normalize_baseline_milestone(milestone)
            previous_release_id = milestone.release_identifier
            previous_successful_release_id = milestone.release_identifier
            plan.append(
                TemporalMilestonePlanEntry(
                    index=index,
                    release_identifier=milestone.release_identifier,
                    previous_release_identifier=None,
                    expected_request_hash=None,
                    cached_response=None,
                    reusable=milestone.status == "complete",
                    blocking_errors=[],
                )
            )
            previous_cumulative = _geometry_from_geojson(milestone.cumulative_union_geojson)
            continue

        previous_identifier = previous_successful_release_id or previous_release_id
        assert previous_identifier is not None
        _, validation_response, prepared = _prepare_temporal_pair_request(
            aoi_geojson=project.aoi_geojson,
            previous_release_identifier=previous_identifier,
            milestone_release_identifier=milestone.release_identifier,
            releases=releases,
            settings=settings,
            remote_patch_budget_enabled=remote_patch_budget_enabled,
            request_hash_context=request_hash_context,
            existing_footprint_geojson=_feature_collection_from_geometry(previous_cumulative),
        )
        expected_request_hash = prepared.request_hash if prepared is not None else None
        cached_response = (
            _load_cached_run_response(settings, expected_request_hash)
            if expected_request_hash is not None
            else None
        )
        artifact_backed_reusable = (
            expected_request_hash is not None
            and milestone.pair_request_hash == expected_request_hash
            and _milestone_has_reusable_additions_artifact(
                project=project,
                milestone=milestone,
                settings=settings,
            )
        )
        reusable = (
            not validation_response.blocking_errors
            and milestone.status == "complete"
            and expected_request_hash is not None
            and milestone.pair_request_hash == expected_request_hash
            and (cached_response is not None or artifact_backed_reusable)
        )
        if artifact_backed_reusable and cached_response is None:
            logger.info(
                "TEMPORAL_PAIR_REUSE_FILE_BACKED_ARTIFACT projectId=%s releaseIdentifier=%s requestHash=%s populatedRequestHash=%s",
                project.project_id,
                milestone.release_identifier,
                expected_request_hash,
                milestone.populated_request_hash,
            )
        plan.append(
            TemporalMilestonePlanEntry(
                index=index,
                release_identifier=milestone.release_identifier,
                previous_release_identifier=previous_identifier,
                expected_request_hash=expected_request_hash,
                cached_response=cached_response,
                reusable=reusable,
                blocking_errors=list(validation_response.blocking_errors),
            )
        )
        if not validation_response.blocking_errors:
            previous_successful_release_id = milestone.release_identifier
            if milestone.status == "complete":
                previous_cumulative = _geometry_from_geojson(milestone.cumulative_union_geojson)
        previous_release_id = milestone.release_identifier

    return plan


def _apply_pair_response_to_milestone(
    milestone: TemporalMilestone,
    *,
    response: RunResponse,
    previous_cumulative: BaseGeometry,
    aoi_geometry: BaseGeometry,
    request_hash: str | None = None,
    populated_request_hash: str | None = None,
    request_workspace_path: str | None = None,
    derive_spatial_layers: bool = True,
    file_backed_additions: bool = False,
) -> None:
    automated_additions_geojson = (
        response.new_buildings_geojson
        or response.change_polygons_geojson
        or _empty_feature_collection()
    )

    response_request_hash = response.summary.request_hash if response.summary is not None else None
    milestone.pair_request_hash = request_hash or response_request_hash
    milestone.populated_request_hash = populated_request_hash or response_request_hash
    milestone.request_workspace_path = request_workspace_path
    milestone.automated_building_blocks_geojson = response.building_blocks_geojson or _empty_feature_collection()
    milestone.buffer_layers_geojson = response.buffer_layers_geojson
    milestone.warnings = [
        warning
        for warning in ((response.diagnostics.warnings if response.diagnostics else []) or [])
        if isinstance(warning, str)
    ]
    if derive_spatial_layers:
        milestone.automated_additions_geojson = automated_additions_geojson
        automated_additions_geometry = _geometry_from_geojson(automated_additions_geojson).intersection(aoi_geometry).buffer(0)
        automated_candidate_geometry = unary_union([previous_cumulative, automated_additions_geometry]).intersection(aoi_geometry).buffer(0)
        milestone.automated_candidate_footprint_geojson = _feature_collection_from_geometry(automated_candidate_geometry)
        return

    milestone.automated_additions_geojson = None
    milestone.automated_candidate_footprint_geojson = None
    milestone.additions_geojson = None if file_backed_additions else automated_additions_geojson
    milestone.effective_footprint_geojson = None
    milestone.cumulative_union_geojson = None
    milestone.effective_building_blocks_geojson = response.building_blocks_geojson or _empty_feature_collection()
    milestone.cumulative_growth_blocks_geojson = response.building_blocks_geojson or _empty_feature_collection()
    milestone.cumulative_growth_envelope_geojson = None
    milestone.metrics = _artifact_backed_metrics(response, automated_additions_geojson)
    milestone.status = "complete"
    milestone.error_message = None


def _should_skip_inline_temporal_derived_geometry(response: RunResponse, settings: Settings) -> bool:
    additions_geojson = response.new_buildings_geojson or response.change_polygons_geojson or _empty_feature_collection()
    feature_count = _feature_count_from_geojson(additions_geojson)
    return feature_count > settings.temporal_derived_geometry_max_features


_CANONICAL_MILESTONE_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("automated_building_blocks.geojson", "Automated building-level blocks"),
    ("additions.geojson", "Effective additions since previous milestone"),
    ("building_change_buffer_10m.geojson", "Building-change buffer 10 m"),
    ("building_change_buffer_15m.geojson", "Building-change buffer 15 m"),
    ("building_change_buffer_20m.geojson", "Building-change buffer 20 m"),
)


def _published_milestone_feature_counts(project_dir: Path, release_identifier: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    milestone_dir = project_dir / "milestones" / release_identifier
    for filename, _description in _CANONICAL_MILESTONE_ARTIFACTS:
        path = milestone_dir / filename
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            counts[filename] = -1
            continue
        features = payload.get("features") if isinstance(payload, dict) else None
        counts[filename] = len(features) if isinstance(features, list) else -1
    return counts


def _milestone_has_reusable_additions_artifact(
    *,
    project: TemporalProject,
    milestone: TemporalMilestone,
    settings: Settings,
) -> bool:
    if milestone.status != "complete" or milestone.metrics is None or milestone.metrics.additions_feature_count <= 0:
        return False
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    candidate_paths = [
        Path(artifact.path)
        for artifact in milestone.artifacts
        if artifact.key == "additions" and artifact.path
    ]
    candidate_paths.append(_artifact_path_for_milestone(project_dir, milestone.release_identifier, "additions.geojson"))
    return any(path.is_file() and path.stat().st_size > 0 for path in candidate_paths)


def _ensure_existing_milestone_artifacts_registered(
    *,
    project: TemporalProject,
    project_dir: Path,
    milestone: TemporalMilestone,
    response: RunResponse,
    feature_counts: dict[str, int],
) -> None:
    milestone.artifacts = _allowed_temporal_artifacts(milestone.artifacts)
    artifact_by_path = {artifact.path: artifact for artifact in milestone.artifacts}
    for filename, description in _CANONICAL_MILESTONE_ARTIFACTS:
        path = project_dir / "milestones" / milestone.release_identifier / filename
        if not path.is_file():
            continue
        artifact_path = str(path)
        artifact_key = next((key for key, (_field, candidate, _description, _media) in TEMPORAL_LAYER_ARTIFACTS.items() if candidate == filename), filename.replace(".geojson", ""))
        entry = _temporal_artifact_entry(
            project_id=project.project_id,
            release_identifier=milestone.release_identifier,
            artifact_key=artifact_key,
            path=path,
            description=description,
            media_type="application/geo+json",
        )
        if artifact_path in artifact_by_path:
            milestone.artifacts = [
                entry if artifact.path == artifact_path else artifact
                for artifact in milestone.artifacts
            ]
        else:
            milestone.artifacts.append(entry)
            artifact_by_path[artifact_path] = entry
        logger.info(
            "TEMPORAL_PROJECT_PUBLICATION_LAYER_WRITTEN projectId=%s releaseIdentifier=%s filename=%s path=%s featureCount=%s source=existing_published_artifact",
            project.project_id,
            milestone.release_identifier,
            filename,
            path,
            feature_counts.get(filename),
        )

def publish_completed_tiled_request(
    *,
    request_id: str,
    project_id: str,
    target_release: str,
    baseline_release: str | None,
    settings: Settings,
) -> dict[str, Any]:
    """Publish an already completed tiled request through the temporal project finalizer.

    This intentionally reuses the existing temporal project recompute and bundle refresh
    path instead of teaching clients to read directly from runtime_cache/requests.
    """

    started_at = time.perf_counter()
    logger.info(
        "TEMPORAL_PROJECT_PUBLICATION_START projectId=%s requestId=%s targetRelease=%s baselineRelease=%s source=completed_tiled_request",
        project_id,
        request_id,
        target_release,
        baseline_release,
    )
    try:
        request_dir = request_result_dir(settings, request_id)
        required_paths = {
            "run_response": request_dir / "run_response.json",
        }
        missing = [name for name, path in required_paths.items() if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"Completed tiled request is missing required outputs: {', '.join(missing)}")

        response = load_cached_response(settings, request_id)
        if response is None or not response.success:
            raise ValueError(f"Cached run response is not successful for request {request_id}.")
        if not _has_features(response.change_polygons_geojson or response.new_buildings_geojson):
            raise ValueError(f"Cached run response has no change polygons for request {request_id}.")
        if response.downloadable_zip_path:
            bundle_path = Path(response.downloadable_zip_path)
        else:
            bundle_path = request_dir / "export_bundle.zip"
            if bundle_path.is_file():
                response.downloadable_zip_path = str(bundle_path)
                save_cached_response(settings, request_id, response)
        logger.info(
            "TEMPORAL_PROJECT_PUBLICATION_INPUTS_VALIDATED projectId=%s requestId=%s requestDir=%s exportBundle=%s",
            project_id,
            request_id,
            request_dir,
            response.downloadable_zip_path,
        )

        project = _load_project(
            settings,
            project_id,
            hydrate_reference_imagery=False,
            hydrate_buffer_layers=False,
            refresh_derived_layers=False,
            write_side_effects=False,
        )
        if project.aoi_geojson is None:
            raise ValueError(f"Temporal project {project_id} has no AOI geometry.")
        _sort_temporal_milestones(project)
        target_index = next(
            (index for index, milestone in enumerate(project.milestones) if milestone.release_identifier == target_release),
            None,
        )
        if target_index is None:
            raise ValueError(f"Target release {target_release} is not present in project {project_id}.")
        if baseline_release is not None and project.milestones:
            first_release = project.milestones[0].release_identifier
            if first_release != baseline_release:
                raise ValueError(
                    f"Baseline release mismatch for {project_id}: expected first milestone {baseline_release}, found {first_release}."
                )

        project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
        target_milestone = project.milestones[target_index]
        existing_feature_counts = _published_milestone_feature_counts(project_dir, target_release)
        fast_path_ready = (
            target_milestone.pair_request_hash == request_id
            and existing_feature_counts.get("additions.geojson", 0) > 0
            and (
                existing_feature_counts.get("building_change_buffer_10m.geojson", 0) > 0
                or target_milestone.metrics is not None
            )
        )
        if fast_path_ready:
            logger.info(
                "TEMPORAL_PROJECT_PUBLICATION_USING_EXISTING_FINALIZER projectId=%s requestId=%s finalizer=existing_milestone_artifact_refresh",
                project_id,
                request_id,
            )
            _ensure_existing_milestone_artifacts_registered(
                project=project,
                project_dir=project_dir,
                milestone=target_milestone,
                response=response,
                feature_counts=existing_feature_counts,
            )
            project.updated_at = _utc_now_iso()
            project.project_dir = str(project_dir)
            externalized_count, empty_baseline_artifacts_removed = _externalize_temporal_artifact_payloads_in_project(
                project=project,
                project_dir=project_dir,
            )
            if externalized_count or empty_baseline_artifacts_removed:
                logger.info(
                    "TEMPORAL_PROJECT_METADATA_PAYLOAD_EXTERNALIZED projectId=%s source=publication externalizedArtifacts=%s emptyBaselineArtifactsRemoved=%s",
                    project.project_id,
                    externalized_count,
                    empty_baseline_artifacts_removed,
                )
            project_json_path = project_dir / "project.json"
            payload = project.model_dump(mode="json")
            project_json_path.write_text(json.dumps(payload, indent=2))
            manifest_path = project_dir / "project_manifest.json"
            manifest_path.write_text(json.dumps(payload, indent=2))
            logger.info(
                "TEMPORAL_PROJECT_PUBLICATION_MANIFEST_UPDATED projectId=%s path=%s",
                project.project_id,
                manifest_path,
            )
            _write_project_summary(project, project_json_path)
            logger.info(
                "TEMPORAL_PROJECT_PUBLICATION_SUMMARY_UPDATED projectId=%s path=%s",
                project.project_id,
                _project_summary_json_path(project_json_path),
            )
        else:
            heavy_required_paths = {
                "building_change_polygons": request_dir / "building_change_polygons.geojson",
                "prediction_change_mask": request_dir / "prediction_change_mask.tif",
                "prediction_change_probability": request_dir / "prediction_change_probability.tif",
            }
            missing_heavy = [name for name, path in heavy_required_paths.items() if not path.is_file()]
            if missing_heavy:
                raise FileNotFoundError(
                    "Completed tiled request is missing required unpublished outputs: "
                    + ", ".join(missing_heavy)
                )
            aoi_geometry = parse_aoi_geometry(project.aoi_geojson)
            if project.milestones:
                _normalize_baseline_milestone(project.milestones[0])
                project = _recompute_project_outputs_from_index(project, aoi_geometry, 0, 0, settings=settings)
            previous_cumulative = (
                GeometryCollection()
                if target_index == 0
                else _geometry_from_geojson(project.milestones[target_index - 1].cumulative_union_geojson)
            )
            inline_derived_geometry_skipped = _should_skip_inline_temporal_derived_geometry(response, settings)
            if inline_derived_geometry_skipped:
                additions_feature_count = _response_additions_feature_count(response)
                logger.info(
                    "TEMPORAL_LARGE_RESULT_POLICY projectId=%s requestId=%s releaseIdentifier=%s featureCount=%s maxInlineDerivedFeatures=%s policy=file_backed_artifacts_continue_without_inline_derived_geometry",
                    project.project_id,
                    request_id,
                    target_release,
                    additions_feature_count,
                    settings.temporal_derived_geometry_max_features,
                )
            file_backed_additions = False
            if inline_derived_geometry_skipped:
                source_additions_path = request_dir / "building_change_polygons.geojson"
                target_additions_path = _artifact_path_for_milestone(project_dir, target_release, "additions.geojson")
                if source_additions_path.is_file():
                    target_additions_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_additions_path, target_additions_path)
                    file_backed_additions = True
                    logger.info(
                        "TEMPORAL_LARGE_ADDITIONS_PROMOTED projectId=%s requestId=%s releaseIdentifier=%s source=%s target=%s sizeBytes=%s",
                        project.project_id,
                        request_id,
                        target_release,
                        source_additions_path,
                        target_additions_path,
                        target_additions_path.stat().st_size,
                    )
                else:
                    logger.warning(
                        "TEMPORAL_LARGE_ADDITIONS_PROMOTION_SKIPPED projectId=%s requestId=%s releaseIdentifier=%s reason=missing_request_geojson source=%s",
                        project.project_id,
                        request_id,
                        target_release,
                        source_additions_path,
                    )
            _apply_pair_response_to_milestone(
                project.milestones[target_index],
                response=response,
                previous_cumulative=previous_cumulative,
                aoi_geometry=aoi_geometry,
                request_hash=request_id,
                populated_request_hash=response.summary.request_hash if response.summary is not None else request_id,
                request_workspace_path=str(request_result_dir(settings, response.summary.request_hash if response.summary is not None else request_id)),
                derive_spatial_layers=not inline_derived_geometry_skipped,
                file_backed_additions=file_backed_additions,
            )
            if inline_derived_geometry_skipped:
                _disable_temporal_growth_envelope(project.project_id, target_release)
            if not inline_derived_geometry_skipped:
                project = _recompute_project_outputs_from_index(project, aoi_geometry, target_index, settings=settings)
            project.updated_at = _utc_now_iso()
            logger.info(
                "TEMPORAL_PROJECT_PUBLICATION_USING_EXISTING_FINALIZER projectId=%s requestId=%s finalizer=_refresh_project_bundle",
                project_id,
                request_id,
            )
            project = _refresh_project_bundle(project, settings)
            project = _save_project(project, settings)
        if settings.persistence_backend == "postgres":
            from src.repositories.temporal_project_repository import save_project as save_project_record

            save_project_record(project, settings=settings)

        artifact_counts: dict[str, int] = {}
        for artifact in target_milestone.artifacts:
            path = Path(artifact.path)
            if path.suffix.lower() == ".geojson" and path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    artifact_counts[path.name] = len(payload.get("features", [])) if isinstance(payload.get("features"), list) else 0
                except Exception:
                    artifact_counts[path.name] = -1
        result = {
            "project_id": project.project_id,
            "request_id": request_id,
            "target_release": target_release,
            "project_dir": str(project_dir),
            "project_json": str(project_dir / "project.json"),
            "project_manifest": str(project_dir / "project_manifest.json"),
            "project_summary": str(project_dir / "project_summary.json"),
            "artifact_counts": artifact_counts,
            "export_bundle_path": response.downloadable_zip_path,
            "inline_derived_geometry_skipped": inline_derived_geometry_skipped if "inline_derived_geometry_skipped" in locals() else False,
        }
        logger.info(
            "TEMPORAL_PROJECT_PUBLICATION_DONE projectId=%s requestId=%s targetRelease=%s artifactCount=%s durationMs=%s",
            project_id,
            request_id,
            target_release,
            len(target_milestone.artifacts),
            round((time.perf_counter() - started_at) * 1000, 2),
        )
        from src.services.request_cleanup import run_post_completion_request_cleanup_if_enabled

        run_post_completion_request_cleanup_if_enabled(
            request_hash=request_id,
            pair_request_hash=target_milestone.pair_request_hash,
            populated_request_hash=target_milestone.populated_request_hash or request_id,
            request_workspace_path=target_milestone.request_workspace_path,
            project_id=project.project_id,
            release_identifier=target_release,
            settings=settings,
        )
        cleanup_finalized_temporal_project_wayback_mosaics(project=project, settings=settings)
        return result
    except Exception as exc:
        logger.exception(
            "TEMPORAL_PROJECT_PUBLICATION_FAILED projectId=%s requestId=%s targetRelease=%s error=%s",
            project_id,
            request_id,
            target_release,
            exc,
        )
        raise


def _buffer_layer_geojson(buffer_layers_geojson: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not buffer_layers_geojson:
        return None

    numeric_layers: list[tuple[float, str, dict[str, Any]]] = []
    for key, value in buffer_layers_geojson.items():
        try:
            distance = float(key)
        except (TypeError, ValueError):
            continue
        numeric_layers.append((distance, key, value))

    if numeric_layers:
        numeric_layers.sort(key=lambda item: item[0])
        return numeric_layers[-1][2]

    return next(iter(buffer_layers_geojson.values()), None)


def _bbox_to_geojson_polygon(bbox: list[float] | tuple[float, float, float, float] | None) -> dict[str, Any] | None:
    if not bbox or len(bbox) < 4:
        return None
    west, south, east, north = bbox[:4]
    return normalized_aoi_geojson(
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        }
    )


def _cached_run_project(settings: Settings, request_hash: str) -> TemporalProject | None:
    response = _load_cached_run_response(settings, request_hash)
    if response is None or not response.success or response.summary is None:
        return None

    result_dir = _cached_run_directory(settings, request_hash)
    pair_summary = _pair_summary_rows(result_dir)
    t1_info = pair_summary.get("t1") if pair_summary else None
    t2_info = pair_summary.get("t2") if pair_summary else None

    t1_identifier = (t1_info or {}).get("identifier") or "T1"
    t2_identifier = (t2_info or {}).get("identifier") or "T2"
    t1_release_date = (t1_info or {}).get("release_date") or response.summary.release_date_t1
    t2_release_date = (t2_info or {}).get("release_date") or response.summary.release_date_t2

    changes_geojson = response.change_polygons_geojson or response.new_buildings_geojson or _empty_feature_collection()
    building_blocks_geojson = response.building_blocks_geojson or _empty_feature_collection()
    request_updated_at = datetime.fromtimestamp(result_dir.joinpath("run_response.json").stat().st_mtime, UTC).isoformat()
    if response.summary.result_semantics == "new_buildings":
        total_area_m2 = response.summary.total_new_building_area_m2
        feature_count = response.summary.total_new_buildings
        block_count = response.summary.total_building_blocks
        block_area_m2 = response.summary.total_building_block_area_m2
    else:
        total_area_m2 = response.summary.total_change_area_m2
        feature_count = response.summary.total_change_polygons
        block_count = response.summary.total_building_blocks
        block_area_m2 = response.summary.total_building_block_area_m2

    if total_area_m2 == 0.0:
        total_area_m2 = response.summary.estimated_area_m2
    if feature_count == 0:
        feature_count = response.summary.total_new_buildings if response.summary.result_semantics == "building_change" else response.summary.total_change_polygons
    if block_count == 0:
        block_count = response.summary.total_new_buildings
    if block_area_m2 == 0.0:
        block_area_m2 = response.summary.total_new_building_area_m2

    aoi_geojson = _bbox_to_geojson_polygon(response.preview_images.raster_bounds_wgs84 if response.preview_images else None)

    target_metrics = TemporalMilestoneMetrics(
        added_area_m2=round(float(total_area_m2 or 0.0), 2),
        total_area_m2=round(float(total_area_m2 or 0.0), 2),
        additions_feature_count=int(feature_count or 0),
        effective_feature_count=int(feature_count or 0),
        building_level_available=response.summary.result_semantics != "building_change",
        added_block_count=int(block_count or 0),
        cumulative_block_count=int(block_count or 0),
        added_block_area_m2=round(float(block_area_m2 or 0.0), 2),
        cumulative_block_area_m2=round(float(block_area_m2 or 0.0), 2),
        growth_envelope_area_m2=0.0,
    )

    baseline = TemporalMilestone(
        release_identifier=t1_identifier,
        release_date=t1_release_date,
        status="complete",
        source_mode="automated",
        warnings=[],
        error_message=None,
        pair_request_hash=None,
        automated_additions_geojson=_empty_feature_collection(),
        automated_candidate_footprint_geojson=_empty_feature_collection(),
        automated_building_blocks_geojson=_empty_feature_collection(),
        manual_override_geojson=None,
        additions_geojson=_empty_feature_collection(),
        effective_building_blocks_geojson=_empty_feature_collection(),
        effective_footprint_geojson=_empty_feature_collection(),
        cumulative_union_geojson=_empty_feature_collection(),
        cumulative_growth_blocks_geojson=_empty_feature_collection(),
        cumulative_growth_envelope_geojson=None,
        metrics=TemporalMilestoneMetrics(),
    )
    target = TemporalMilestone(
        release_identifier=t2_identifier,
        release_date=t2_release_date,
        status="complete",
        source_mode="automated",
        warnings=[warning for warning in ((response.diagnostics.warnings if response.diagnostics else []) or []) if isinstance(warning, str)],
        error_message=response.error_message,
        pair_request_hash=request_hash,
        automated_additions_geojson=changes_geojson,
        automated_candidate_footprint_geojson=changes_geojson,
        automated_building_blocks_geojson=building_blocks_geojson,
        manual_override_geojson=None,
        additions_geojson=changes_geojson,
        effective_building_blocks_geojson=building_blocks_geojson,
        effective_footprint_geojson=changes_geojson,
        cumulative_union_geojson=changes_geojson,
        cumulative_growth_blocks_geojson=building_blocks_geojson,
        cumulative_growth_envelope_geojson=None,
        reference_imagery=None,
        metrics=target_metrics,
        artifacts=[
            TemporalArtifactEntry.model_validate(item.model_dump(mode="json") if hasattr(item, "model_dump") else item)
            for item in response.artifacts
        ],
    )

    return TemporalProject(
        project_id=_run_project_id(request_hash),
        name=f"{t1_identifier} → {t2_identifier}",
        semantics="expansion_only",
        aoi_geojson=aoi_geojson,
        milestones=[baseline, target],
        created_at=request_updated_at,
        updated_at=request_updated_at,
        warnings=[],
        validation_blocking_errors=[],
        download_bundle_path=response.downloadable_zip_path,
    )


def _project_kind(project_id: str) -> str:
    return "pairwise" if project_id.startswith("run-") else "temporal"


def _project_display_name(project: TemporalProject) -> str:
    if project.project_id.startswith("run-"):
        return f"Pairwise · {project.name}"
    if project.name and project.name != "Untitled Temporal Mosaic":
        return f"Temporal mosaic · {project.name}"
    milestone_count = len(project.milestones)
    if milestone_count == 1:
        return "Temporal mosaic · 1 milestone"
    return f"Temporal mosaic · {milestone_count} milestones"


def _project_summary_display_name(project_id: str, name: str, milestone_count: int) -> str:
    if project_id.startswith("run-"):
        return f"Pairwise · {name}"
    if name and name != "Untitled Temporal Mosaic":
        return f"Temporal mosaic · {name}"
    if milestone_count == 1:
        return "Temporal mosaic · 1 milestone"
    return f"Temporal mosaic · {milestone_count} milestones"


def _project_summary(project: TemporalProject, *, project_dir: str | None = None) -> TemporalProjectSummary:
    resolved_project_dir = project.project_dir or project_dir
    if resolved_project_dir is not None:
        project.project_dir = resolved_project_dir
    return TemporalProjectSummary(
        project_id=project.project_id,
        name=project.name,
        project_dir=resolved_project_dir,
        project_kind=_project_kind(project.project_id),
        display_name=_project_display_name(project),
        semantics=project.semantics,
        milestone_count=len(project.milestones),
        complete_milestone_count=sum(1 for item in project.milestones if item.status == "complete"),
        created_at=project.created_at,
        updated_at=project.updated_at,
        download_bundle_path=project.download_bundle_path,
    )


def _write_project_summary(project: TemporalProject, project_json_path: Path) -> None:
    summary = _project_summary(project, project_dir=str(project_json_path.parent))
    _project_summary_json_path(project_json_path).write_text(json.dumps(summary.model_dump(mode="json"), indent=2))
    _write_project_compact_metadata(project, project_json_path)


def _load_cached_project_summary(
    project_json_path: Path,
    *,
    expected_project_id: str | None = None,
) -> TemporalProjectSummary | None:
    summary_path = _project_summary_json_path(project_json_path)
    if not summary_path.exists():
        return None
    try:
        if summary_path.stat().st_mtime < project_json_path.stat().st_mtime:
            return None
        summary = TemporalProjectSummary.model_validate(json.loads(summary_path.read_text()))
    except Exception:
        return None
    if expected_project_id is not None and summary.project_id != expected_project_id:
        return None
    return summary


def _load_saved_project_summary(
    project_json_path: Path,
    *,
    expected_project_id: str | None = None,
) -> TemporalProjectSummary | None:
    cached_summary = _load_cached_project_summary(project_json_path, expected_project_id=expected_project_id)
    if cached_summary is not None:
        return cached_summary

    try:
        project = validate_stored_temporal_project(json.loads(project_json_path.read_text()))
    except Exception:
        return None

    if expected_project_id is not None and project.project_id != expected_project_id:
        return None

    try:
        _write_project_summary(project, project_json_path)
    except Exception:
        pass
    return _project_summary(project, project_dir=str(project_json_path.parent))


def _iter_cached_run_projects(settings: Settings) -> list[TemporalProjectSummary]:
    summaries: list[TemporalProjectSummary] = []
    for response_path in settings.request_cache_dir.glob("*/run_response.json"):
        request_hash = response_path.parent.name
        response = _load_cached_run_response(settings, request_hash)
        if response is None or not response.success or response.summary is None:
            continue
        pair_summary = _pair_summary_rows(response_path.parent)
        t1_row = pair_summary.get("t1") if pair_summary else None
        t2_row = pair_summary.get("t2") if pair_summary else None
        t1_identifier = (t1_row or {}).get("identifier") or "T1"
        t2_identifier = (t2_row or {}).get("identifier") or "T2"
        summary_name = f"{t1_identifier} → {t2_identifier}"
        try:
            updated_at = datetime.fromtimestamp(response_path.stat().st_mtime, UTC).isoformat()
        except OSError:
            updated_at = _utc_now_iso()
        summaries.append(
            TemporalProjectSummary(
                project_id=_run_project_id(request_hash),
                name=summary_name,
                project_kind="pairwise",
                display_name=_project_summary_display_name(_run_project_id(request_hash), summary_name, 2),
                semantics="expansion_only",
                milestone_count=2,
                complete_milestone_count=2,
                created_at=updated_at,
                updated_at=updated_at,
                download_bundle_path=response.downloadable_zip_path,
            )
        )
    return summaries


def _timeline_requests(
    project: TemporalProject,
    *,
    settings: Settings,
    remote_patch_budget_enabled: bool,
    request_hash_context: dict[str, object] | None,
) -> tuple[list[TemporalPairEstimate], list[str], list[str]]:
    pair_estimates: list[TemporalPairEstimate] = []
    warnings: list[str] = []
    blocking_errors: list[str] = []
    if project.aoi_geojson is None:
        return pair_estimates, warnings, ["AOI is required before validating a temporal project."]

    releases = list_releases(settings)
    releases_by_id = {release.identifier: release for release in releases}
    seen: set[str] = set()
    previous_release_id: str | None = None
    previous_successful_release_id: str | None = None
    previous_release_date = None

    last_index = len(project.milestones) - 1
    for index, milestone in enumerate(project.milestones):
        release = releases_by_id.get(milestone.release_identifier)
        if release is None:
            blocking_errors.append(f"Unknown Wayback release: {milestone.release_identifier}")
            continue

        milestone.release_date = str(release.release_date)
        if milestone.release_identifier in seen:
            blocking_errors.append(f"Duplicate milestone release: {milestone.release_identifier}")
        seen.add(milestone.release_identifier)

        if previous_release_date is not None and release.release_date <= previous_release_date:
            blocking_errors.append("Milestones must be in strictly chronological order.")
        previous_release_date = release.release_date

        if index == 0 and milestone.manual_override_geojson is None:
            warnings.append(
                f"Baseline milestone {milestone.release_identifier} has no manual override; cumulative growth will start from an empty baseline."
            )
        if previous_release_id is None:
            previous_release_id = milestone.release_identifier
            previous_successful_release_id = milestone.release_identifier
            milestone.status = "validated" if not milestone.error_message else "error"
            continue

        pair_source_release_id = previous_successful_release_id or previous_release_id
        validation_request_kwargs: dict[str, Any] = {
            "aoi_geojson": project.aoi_geojson,
            "t1_release": pair_source_release_id,
            "t2_release": release.identifier,
            "mode": "full_run",
        }
        if request_hash_context and request_hash_context.get("threshold_source") == "request_override":
            validation_request_kwargs["change_threshold"] = float(request_hash_context["change_threshold"])
        validation_request = ValidationRequest(**validation_request_kwargs)
        validation_response, prepared = validate_request(
            validation_request,
            releases=releases,
            settings=settings,
            remote_patch_budget_enabled=remote_patch_budget_enabled,
            request_hash_context=request_hash_context,
        )
        pair_estimates.append(
            TemporalPairEstimate(
                from_release_identifier=pair_source_release_id,
                to_release_identifier=milestone.release_identifier,
                estimated_tile_count_t1=validation_response.estimated_tile_count_t1,
                estimated_tile_count_t2=validation_response.estimated_tile_count_t2,
                estimated_total_tiles=validation_response.estimated_total_tiles,
                warnings=validation_response.warnings,
                blocking_errors=validation_response.blocking_errors,
            )
        )
        if validation_response.blocking_errors:
            warnings.extend(
                f"{pair_source_release_id} -> {milestone.release_identifier}: {message}"
                for message in validation_response.blocking_errors
            )
        warnings.extend(
            f"{pair_source_release_id} -> {milestone.release_identifier}: {message}"
            for message in validation_response.warnings
        )
        expected_request_hash = prepared.request_hash if prepared is not None else None
        completed_pair_still_reusable = (
            not validation_response.blocking_errors
            and milestone.status == "complete"
            and expected_request_hash is not None
            and milestone.pair_request_hash == expected_request_hash
            and (
                _load_cached_run_response(settings, expected_request_hash) is not None
                or _milestone_has_reusable_additions_artifact(
                    project=project,
                    milestone=milestone,
                    settings=settings,
                )
            )
        )
        if completed_pair_still_reusable:
            milestone.status = "complete"
        else:
            milestone.status = "validated" if not validation_response.blocking_errors else "error"
        if not validation_response.blocking_errors:
            previous_successful_release_id = milestone.release_identifier
        previous_release_id = milestone.release_identifier

    return pair_estimates, warnings, blocking_errors


def list_temporal_projects(settings: Settings, *, include_cached_runs: bool = False) -> list[TemporalProjectSummary]:
    if settings.persistence_backend == "postgres":
        from src.repositories.temporal_project_repository import list_project_summaries

        summaries = list_project_summaries(settings=settings)
        if include_cached_runs:
            seen_project_ids = {summary.project_id for summary in summaries}
            for cached_run_summary in _iter_cached_run_projects(settings):
                if cached_run_summary.project_id not in seen_project_ids:
                    summaries.append(cached_run_summary)
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries

    summaries: list[TemporalProjectSummary] = []
    seen_project_ids: set[str] = set()
    registry = _load_project_registry(settings)
    for registry_project_id, registry_project_dir in registry.items():
        project_json_path = Path(registry_project_dir) / "project.json"
        if not project_json_path.exists():
            continue
        summary = _load_saved_project_summary(project_json_path, expected_project_id=registry_project_id)
        if summary is None or summary.project_id in seen_project_ids:
            continue
        seen_project_ids.add(summary.project_id)
        summaries.append(summary)
    for project_json_path in settings.temporal_projects_dir.glob("*/project.json"):
        summary = _load_saved_project_summary(project_json_path)
        if summary is None or summary.project_id in seen_project_ids:
            continue
        seen_project_ids.add(summary.project_id)
        summaries.append(summary)
    if include_cached_runs:
        for cached_run_summary in _iter_cached_run_projects(settings):
            if cached_run_summary.project_id in seen_project_ids:
                continue
            summaries.append(cached_run_summary)
    summaries.sort(key=lambda item: item.updated_at, reverse=True)
    return summaries


def get_temporal_project(project_id: str, settings: Settings) -> TemporalProject:
    try:
        return _load_project(
            settings,
            project_id,
            hydrate_reference_imagery=False,
            hydrate_buffer_layers=False,
            refresh_derived_layers=False,
            write_side_effects=False,
        )
    except FileNotFoundError:
        if project_id.startswith("run-"):
            cached_project = _cached_run_project(settings, project_id.removeprefix("run-"))
            if cached_project is not None:
                return cached_project
        raise


def save_temporal_project(project: TemporalProject, settings: Settings) -> TemporalProject:
    _safe_project_id(project.project_id)
    normalized = project.model_copy(deep=True)
    if not normalized.created_at:
        normalized.created_at = _utc_now_iso()
    if normalized.aoi_geojson is not None:
        normalized.aoi_geojson = normalized_aoi_geojson(normalized.aoi_geojson)
    saved_project = _save_project(normalized, settings)
    if settings.persistence_backend == "postgres":
        from src.repositories.temporal_project_repository import save_project as save_project_record

        save_project_record(saved_project, settings=settings)
    return saved_project


def validate_temporal_project(
    project: TemporalProject,
    *,
    settings: Settings,
    remote_patch_budget_enabled: bool = True,
    request_hash_context: dict[str, object] | None = None,
    execution_config: PipelineExecutionConfig | None = None,
) -> TemporalProjectValidationResponse:
    normalized = project.model_copy(deep=True)
    normalized.execution_config = execution_config or resolve_temporal_project_execution_config(normalized, settings)
    normalized = _populate_milestone_release_dates(normalized, settings)
    if request_hash_context is None:
        backend = resolve_backend(normalized.execution_config, settings=settings)
        settings = backend.configure_settings(settings)
        remote_patch_budget_enabled = backend.enforce_remote_patch_budget()
        request_hash_context = backend.request_hash_context(settings)
    project_warnings: list[str] = []
    blocking_errors: list[str] = []

    if normalized.aoi_geojson is not None:
        try:
            normalized.aoi_geojson = normalized_aoi_geojson(normalized.aoi_geojson)
            aoi_geometry = parse_aoi_geometry(normalized.aoi_geojson)
            for milestone in normalized.milestones:
                if milestone.manual_override_geojson is not None:
                    milestone.manual_override_geojson = _normalize_project_geometry(
                        milestone.manual_override_geojson,
                        aoi_geometry,
                    )
        except ValueError as exc:
            blocking_errors.append(str(exc))

    if not normalized.milestones:
        blocking_errors.append("At least one milestone is required.")
    elif normalized.milestones:
        _normalize_baseline_milestone(normalized.milestones[0])

    if not blocking_errors:
        pair_estimates, pair_warnings, pair_blocking_errors = _timeline_requests(
            normalized,
            settings=settings,
            remote_patch_budget_enabled=remote_patch_budget_enabled,
            request_hash_context=request_hash_context,
        )
        project_warnings.extend(pair_warnings)
        blocking_errors.extend(pair_blocking_errors)
    else:
        pair_estimates = []

    normalized.warnings = project_warnings
    normalized.validation_blocking_errors = blocking_errors
    estimated_total_tiles = sum(item.estimated_total_tiles for item in pair_estimates)

    return TemporalProjectValidationResponse(
        valid=not blocking_errors,
        project=normalized,
        warnings=project_warnings,
        blocking_errors=blocking_errors,
        estimated_total_tiles=estimated_total_tiles,
        pair_estimates=pair_estimates,
    )


def _recompute_project_outputs_from_index(
    project: TemporalProject,
    aoi_geometry: BaseGeometry,
    start_index: int,
    end_index: int | None = None,
    settings: Settings | None = None,
) -> TemporalProject:
    _sort_temporal_milestones(project)
    if not project.milestones:
        return project

    start_index = max(start_index, 0)
    if start_index >= len(project.milestones):
        return project
    if end_index is None:
        end_index = len(project.milestones) - 1
    else:
        end_index = min(max(end_index, start_index), len(project.milestones) - 1)

    previous_cumulative = (
        GeometryCollection()
        if start_index == 0
        else _geometry_from_geojson(project.milestones[start_index - 1].cumulative_union_geojson)
    )

    for index in range(start_index, end_index + 1):
        milestone = project.milestones[index]
        max_features = settings.temporal_derived_geometry_max_features if settings is not None else None
        if max_features is not None and _feature_count_from_geojson(milestone.automated_additions_geojson) > max_features:
            additions_geojson = milestone.automated_additions_geojson or _empty_feature_collection()
            milestone.additions_geojson = additions_geojson
            milestone.effective_footprint_geojson = None
            milestone.cumulative_union_geojson = None
            milestone.effective_building_blocks_geojson = milestone.automated_building_blocks_geojson or _empty_feature_collection()
            milestone.cumulative_growth_blocks_geojson = milestone.automated_building_blocks_geojson or _empty_feature_collection()
            milestone.cumulative_growth_envelope_geojson = None
            if milestone.metrics is None:
                milestone.metrics = TemporalMilestoneMetrics(
                    additions_feature_count=_feature_count_from_geojson(additions_geojson),
                    effective_feature_count=_feature_count_from_geojson(additions_geojson),
                )
            else:
                milestone.metrics.growth_envelope_area_m2 = 0.0
            if milestone.status != "error":
                milestone.status = "complete"
                milestone.error_message = None
            logger.info(
                "TEMPORAL_RECOMPUTE_OPTIONAL_DERIVED_SKIPPED projectId=%s releaseIdentifier=%s reason=large_artifact_backed_result featureCount=%s maxInlineDerivedFeatures=%s",
                project.project_id,
                milestone.release_identifier,
                _feature_count_from_geojson(additions_geojson),
                max_features,
            )
            _disable_temporal_growth_envelope(project.project_id, milestone.release_identifier)
            previous_cumulative = GeometryCollection()
            continue

        release_date = milestone.release_date
        automated_candidate_geometry = _geometry_from_geojson(milestone.automated_candidate_footprint_geojson)
        if automated_candidate_geometry.is_empty and milestone.automated_additions_geojson is not None:
            automated_additions_geometry = _geometry_from_geojson(milestone.automated_additions_geojson)
            automated_candidate_geometry = unary_union([previous_cumulative, automated_additions_geometry]).buffer(0)

        manual_geometry = _geometry_from_geojson(milestone.manual_override_geojson)
        if not manual_geometry.is_empty:
            candidate_geometry = manual_geometry
            source_mode = "hybrid_reviewed" if not automated_candidate_geometry.is_empty else "manual_override"
        else:
            candidate_geometry = automated_candidate_geometry
            source_mode = "automated"

        if candidate_geometry.is_empty and index == 0:
            milestone.warnings = [
                message
                for message in milestone.warnings
                if "empty baseline" not in message.lower()
            ]
            milestone.warnings.append(
                "Temporal Mosaic is using an empty baseline for the first milestone because no manual override was provided."
            )
        effective_geometry = unary_union([previous_cumulative, candidate_geometry]).intersection(aoi_geometry).buffer(0)
        additions_geometry = effective_geometry.difference(previous_cumulative).buffer(0)

        if candidate_geometry.is_empty and manual_geometry.is_empty and milestone.status == "pending":
            milestone.additions_geojson = None
            milestone.effective_footprint_geojson = None
            milestone.cumulative_union_geojson = None
            milestone.effective_building_blocks_geojson = None
            milestone.cumulative_growth_blocks_geojson = None
            milestone.cumulative_growth_envelope_geojson = None
            milestone.metrics = TemporalMilestoneMetrics()
            previous_cumulative = effective_geometry
            continue

        milestone.source_mode = source_mode
        milestone.additions_geojson = _feature_collection_from_geometry(additions_geometry)
        milestone.effective_footprint_geojson = _feature_collection_from_geometry(effective_geometry)
        milestone.cumulative_union_geojson = _feature_collection_from_geometry(effective_geometry)

        if project.aoi_geojson is not None:
            _effective_blocks_df, effective_blocks_geojson = build_temporal_growth_blocks(
                milestone.additions_geojson,
                aoi_geojson=project.aoi_geojson,
                release_identifier=milestone.release_identifier,
                release_date=release_date,
                kind="effective_building_block",
            )
            _cumulative_blocks_df, cumulative_blocks_geojson = build_temporal_growth_blocks(
                milestone.cumulative_union_geojson,
                aoi_geojson=project.aoi_geojson,
                release_identifier=milestone.release_identifier,
                release_date=release_date,
                kind="cumulative_growth_block",
            )
            milestone.effective_building_blocks_geojson = effective_blocks_geojson
            milestone.cumulative_growth_blocks_geojson = cumulative_blocks_geojson
            milestone.cumulative_growth_envelope_geojson = None
        else:
            milestone.effective_building_blocks_geojson = _empty_feature_collection()
            milestone.cumulative_growth_blocks_geojson = _empty_feature_collection()
            milestone.cumulative_growth_envelope_geojson = None
        _disable_temporal_growth_envelope(project.project_id, milestone.release_identifier)

        milestone.metrics = _build_metrics(
            additions_geometry,
            effective_geometry,
            building_level_available=manual_geometry.is_empty,
            effective_building_blocks_geojson=milestone.effective_building_blocks_geojson,
            cumulative_growth_blocks_geojson=milestone.cumulative_growth_blocks_geojson,
            cumulative_growth_envelope_geojson=None,
        )
        if milestone.status != "error":
            milestone.status = "complete"
            milestone.error_message = None
        previous_cumulative = effective_geometry

    return project


def _recompute_project_outputs(project: TemporalProject, aoi_geometry: BaseGeometry) -> TemporalProject:
    return _recompute_project_outputs_from_index(project, aoi_geometry, 0)


def _storage_size_bytes(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if not child.is_file() and not child.is_symlink():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            pass
    return total


def _wayback_mosaic_storage_size_for_milestone(milestone: TemporalMilestone) -> tuple[str | None, int]:
    reference = milestone.reference_imagery
    canonical_cog_path = Path(reference.canonical_cog_path) if reference and reference.canonical_cog_path else None
    if canonical_cog_path is None:
        return None, 0
    metadata = read_reference_imagery_cache_metadata(canonical_cog_path.with_name("metadata.json"))
    source_dir = metadata.get("source_wayback_mosaic_dir") if metadata else None
    if not isinstance(source_dir, str) or not source_dir:
        return None, 0
    source_path = Path(source_dir)
    return str(source_path), _storage_size_bytes(source_path)


def _cleanup_report_paths(entries: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        value = entry.get("path")
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _cleanup_published_temporal_pair_request(
    *,
    project: TemporalProject,
    milestone: TemporalMilestone,
    settings: Settings,
) -> None:
    request_hash = milestone.pair_request_hash or milestone.populated_request_hash
    if not request_hash:
        return
    physical_request_hash = milestone.populated_request_hash or request_hash
    request_dir = Path(milestone.request_workspace_path) if milestone.request_workspace_path else settings.request_cache_dir / physical_request_hash
    project_dir = Path(project.project_dir) if project.project_dir else settings.temporal_projects_dir / project.project_id
    milestone_dir = project_dir / "milestones" / milestone.release_identifier
    wayback_mosaic_dir, wayback_mosaic_size_bytes = _wayback_mosaic_storage_size_for_milestone(milestone)
    request_size_before = _storage_size_bytes(request_dir)
    logger.info(
        "TEMPORAL_PAIR_STORAGE_ACCOUNTING_BEFORE projectId=%s releaseIdentifier=%s requestHash=%s populatedRequestHash=%s requestDir=%s requestSizeBytes=%s milestoneDir=%s milestoneSizeBytes=%s waybackMosaicDir=%s waybackMosaicSizeBytes=%s",
        project.project_id,
        milestone.release_identifier,
        request_hash,
        physical_request_hash,
        request_dir,
        request_size_before,
        milestone_dir,
        _storage_size_bytes(milestone_dir),
        wayback_mosaic_dir,
        wayback_mosaic_size_bytes,
    )
    from src.services.request_cleanup import run_post_completion_request_cleanup_if_enabled

    report = None
    cleanup_error: str | None = None
    try:
        report = run_post_completion_request_cleanup_if_enabled(
            request_hash=request_hash,
            pair_request_hash=milestone.pair_request_hash,
            populated_request_hash=milestone.populated_request_hash,
            request_workspace_path=milestone.request_workspace_path,
            project_id=project.project_id,
            release_identifier=milestone.release_identifier,
            settings=settings,
        )
    except Exception as exc:
        cleanup_error = str(exc)
        logger.warning(
            "TEMPORAL_PAIR_REQUEST_CLEANUP_FAILED projectId=%s releaseIdentifier=%s requestHash=%s populatedRequestHash=%s error=%s",
            project.project_id,
            milestone.release_identifier,
            request_hash,
            physical_request_hash,
            exc,
            exc_info=True,
        )
    request_size_after = _storage_size_bytes(request_dir)
    preserved_paths: list[str] = []
    deleted_paths: list[str] = []
    if report is not None:
        preserved_paths = _cleanup_report_paths(report.preserved) + _cleanup_report_paths(report.preserved_request_files)
        deleted_paths = _cleanup_report_paths(report.deleted)
    logger.info(
        "TEMPORAL_PAIR_STORAGE_ACCOUNTING_AFTER projectId=%s releaseIdentifier=%s requestHash=%s populatedRequestHash=%s cleanupMode=%s cleanupSkipped=%s cleanupReason=%s bytesDeleted=%s requestSizeBeforeBytes=%s requestSizeAfterBytes=%s requestBytesFreedObserved=%s preservedCount=%s deletedCount=%s preservedPaths=%s deletedPaths=%s",
        project.project_id,
        milestone.release_identifier,
        request_hash,
        physical_request_hash,
        report.mode if report is not None else settings.post_completion_request_cleanup_mode,
        report.skipped if report is not None else True,
        report.reason if report is not None else cleanup_error or "cleanup_disabled_or_failed",
        report.bytes_deleted if report is not None else 0,
        request_size_before,
        request_size_after,
        max(request_size_before - request_size_after, 0),
        len(preserved_paths),
        len(deleted_paths),
        json.dumps(preserved_paths, sort_keys=True),
        json.dumps(deleted_paths, sort_keys=True),
    )


def run_temporal_project(
    project_id: str,
    *,
    settings: Settings,
    pair_runner: PairRunner,
    remote_patch_budget_enabled: bool = True,
    request_hash_context: dict[str, object] | None = None,
    execution_config: PipelineExecutionConfig | None = None,
) -> TemporalProjectRunResponse:
    project = _load_project(settings, project_id)
    timing = StageTimingRecorder(
        run_id=project.project_id,
        pipeline_kind="temporal_project",
        project_id=project.project_id,
        metadata={"milestone_count": len(project.milestones)},
    )
    resolved_execution_config = execution_config or resolve_temporal_project_execution_config(project, settings)
    if request_hash_context is None:
        backend = resolve_backend(resolved_execution_config, settings=settings)
        settings = backend.configure_settings(settings)
        remote_patch_budget_enabled = backend.enforce_remote_patch_budget()
        request_hash_context = backend.request_hash_context(settings)
    logger.info("EFFECTIVE_INFERENCE_BACKEND value=%s projectId=%s", settings.inference_backend, project.project_id)
    logger.info(
        "EFFECTIVE_CHECKPOINT_PATH value=%s projectId=%s",
        request_hash_context.get("checkpoint_path") if request_hash_context else None,
        project.project_id,
    )
    logger.info(
        "EFFECTIVE_CHECKPOINT_SHA256 value=%s projectId=%s",
        request_hash_context.get("checkpoint_sha256") if request_hash_context else None,
        project.project_id,
    )
    logger.info(
        "EFFECTIVE_THRESHOLD value=%s projectId=%s",
        request_hash_context.get("change_threshold") if request_hash_context else None,
        project.project_id,
    )
    validation = validate_temporal_project(
        project,
        settings=settings,
        remote_patch_budget_enabled=remote_patch_budget_enabled,
        request_hash_context=request_hash_context,
        execution_config=resolved_execution_config,
    )
    project = validation.project
    if not validation.valid:
        _save_project(project, settings)
        _write_temporal_project_timing_safely(timing, project)
        return TemporalProjectRunResponse(
            success=False,
            error_message="; ".join(validation.blocking_errors) or "Temporal project validation failed.",
            project=project,
        )

    assert project.aoi_geojson is not None
    aoi_geometry = parse_aoi_geometry(project.aoi_geojson)
    _normalize_baseline_milestone(project.milestones[0])
    project.milestones[0].automated_additions_geojson = _empty_feature_collection()
    project.milestones[0].automated_candidate_footprint_geojson = _empty_feature_collection()
    project.milestones[0].automated_building_blocks_geojson = _empty_feature_collection()
    project.milestones[0].buffer_layers_geojson = {}
    project = _recompute_project_outputs_from_index(project, aoi_geometry, 0, 0, settings=settings)
    plan = _plan_temporal_milestone_runs(
        project,
        settings=settings,
        remote_patch_budget_enabled=remote_patch_budget_enabled,
        request_hash_context=request_hash_context,
    )
    prefetch_results = _run_temporal_imagery_prefetch(
        project,
        settings=settings,
        pair_plan=plan,
        timing=timing,
    )
    prefetch_warnings = [item.warning for item in prefetch_results if item.warning]
    if prefetch_warnings:
        project.warnings = [
            *project.warnings,
            *[
                f"Temporal imagery prefetch failed for pair {item.pair_index}: {item.warning}"
                for item in prefetch_results
                if item.warning
            ],
        ]
    dirty_start = next(
        (
            entry.index
            for entry in plan
            if entry.index > 0 and not entry.reusable
        ),
        None,
    )

    if dirty_start is None:
        project.updated_at = _utc_now_iso()
        project = _refresh_project_bundle(project, settings)
        _save_project(project, settings)
        _write_temporal_project_timing_safely(timing, project)
        cleanup_finalized_temporal_project_wayback_mosaics(project=project, settings=settings)
        return TemporalProjectRunResponse(success=True, project=project)

    previous_successful_release_identifier = project.milestones[dirty_start - 1].release_identifier if dirty_start > 0 else None
    previous_cumulative = GeometryCollection()
    if dirty_start > 0:
        previous_milestone = project.milestones[dirty_start - 1]
        if not _is_large_temporal_payload(previous_milestone.cumulative_union_geojson, settings):
            previous_cumulative = _geometry_from_geojson(previous_milestone.cumulative_union_geojson)
        previous_large_feature_count = _milestone_large_result_feature_count(previous_milestone, settings)
        if previous_large_feature_count:
            logger.info(
                "TEMPORAL_PAIR_CONTINUE_AFTER_LARGE_RESULT projectId=%s previousRelease=%s nextRelease=%s featureCount=%s maxInlineDerivedFeatures=%s policy=use_file_backed_previous_outputs",
                project.project_id,
                previous_milestone.release_identifier,
                project.milestones[dirty_start].release_identifier,
                previous_large_feature_count,
                settings.temporal_derived_geometry_max_features,
            )

    releases = list_releases(settings)
    for index in range(dirty_start, len(project.milestones)):
        milestone = project.milestones[index]
        milestone.warnings = []
        milestone.error_message = None

        previous_release_identifier = previous_successful_release_identifier or project.milestones[index - 1].release_identifier
        run_request, validation_response, prepared = _prepare_temporal_pair_request(
            aoi_geojson=project.aoi_geojson,
            previous_release_identifier=previous_release_identifier,
            milestone_release_identifier=milestone.release_identifier,
            releases=releases,
            settings=settings,
            remote_patch_budget_enabled=remote_patch_budget_enabled,
            request_hash_context=request_hash_context,
            existing_footprint_geojson=_feature_collection_from_geometry(previous_cumulative),
        )
        if prepared is None or validation_response.blocking_errors:
            milestone.status = "error"
            milestone.error_message = "; ".join(validation_response.blocking_errors) or "Temporal pair validation failed."
            project.updated_at = _utc_now_iso()
            continue

        cached_response = _load_cached_run_response(settings, prepared.request_hash)
        run_request_kwargs: dict[str, Any] = {
            "aoi_geojson": run_request.aoi_geojson,
            "t1_release": run_request.t1_release,
            "t2_release": run_request.t2_release,
            "mode": run_request.mode,
            "existing_footprint_geojson": run_request.existing_footprint_geojson,
        }
        if change_threshold_was_explicit(run_request):
            run_request_kwargs["change_threshold"] = run_request.change_threshold
        response = cached_response if cached_response is not None else pair_runner(RunRequest(**run_request_kwargs))
        if response is None or not response.success:
            milestone.status = "error"
            milestone.error_message = (response.error_message if response is not None else None) or "Temporal pair run failed."
            project.updated_at = _utc_now_iso()
            continue

        inline_derived_geometry_skipped = _should_skip_inline_temporal_derived_geometry(response, settings)
        if inline_derived_geometry_skipped:
            additions_feature_count = _response_additions_feature_count(response)
            logger.info(
                "TEMPORAL_LARGE_RESULT_POLICY projectId=%s releaseIdentifier=%s requestHash=%s featureCount=%s maxInlineDerivedFeatures=%s policy=file_backed_artifacts_continue_without_inline_derived_geometry",
                project.project_id,
                milestone.release_identifier,
                prepared.request_hash,
                additions_feature_count,
                settings.temporal_derived_geometry_max_features,
            )
        _apply_pair_response_to_milestone(
            milestone,
            response=response,
            previous_cumulative=previous_cumulative,
            aoi_geometry=aoi_geometry,
            request_hash=prepared.request_hash,
            populated_request_hash=response.summary.request_hash if response.summary is not None else None,
            request_workspace_path=str(request_result_dir(settings, response.summary.request_hash))
            if response.summary is not None
            else None,
            derive_spatial_layers=not inline_derived_geometry_skipped,
        )
        if not inline_derived_geometry_skipped:
            project = _recompute_project_outputs_from_index(project, aoi_geometry, index, index, settings=settings)
        logger.info(
            "TEMPORAL_SUMMARY_SOURCE projectId=%s milestone=%s sourceRunId=%s changeThreshold=%s "
            "thresholdSource=%s semanticThreshold=%s totalAreaM2=%s",
            project.project_id,
            milestone.release_identifier,
            prepared.request_hash,
            request_hash_context.get("change_threshold") if request_hash_context else None,
            request_hash_context.get("threshold_source") if request_hash_context else None,
            settings.semantic_threshold,
            milestone.metrics.total_area_m2 if milestone.metrics is not None else None,
        )
        project.updated_at = _utc_now_iso()
        project = _refresh_project_bundle(project, settings)
        project = _save_project(project, settings)
        persisted_milestone = project.milestones[index]
        _cleanup_published_temporal_pair_request(
            project=project,
            milestone=persisted_milestone,
            settings=settings,
        )
        previous_cumulative = (
            GeometryCollection()
            if inline_derived_geometry_skipped or _is_large_temporal_payload(persisted_milestone.cumulative_union_geojson, settings)
            else _geometry_from_geojson(persisted_milestone.cumulative_union_geojson)
        )
        if inline_derived_geometry_skipped and index + 1 < len(project.milestones):
            logger.info(
                "TEMPORAL_PAIR_CONTINUE_AFTER_LARGE_RESULT projectId=%s previousRelease=%s nextRelease=%s featureCount=%s maxInlineDerivedFeatures=%s policy=use_file_backed_previous_outputs",
                project.project_id,
                persisted_milestone.release_identifier,
                project.milestones[index + 1].release_identifier,
                _response_additions_feature_count(response),
                settings.temporal_derived_geometry_max_features,
            )
        previous_successful_release_identifier = persisted_milestone.release_identifier

    project.updated_at = _utc_now_iso()
    project = _refresh_project_bundle(project, settings)
    _save_project(project, settings)
    _write_temporal_project_timing_safely(timing, project)
    from src.services.request_cleanup import run_post_completion_request_cleanup_if_enabled

    for milestone in project.milestones:
        if milestone.status == "complete" and (milestone.populated_request_hash or milestone.pair_request_hash):
            run_post_completion_request_cleanup_if_enabled(
                request_hash=milestone.pair_request_hash or milestone.populated_request_hash,
                pair_request_hash=milestone.pair_request_hash,
                populated_request_hash=milestone.populated_request_hash,
                request_workspace_path=milestone.request_workspace_path,
                project_id=project.project_id,
                release_identifier=milestone.release_identifier,
                settings=settings,
            )
    cleanup_finalized_temporal_project_wayback_mosaics(project=project, settings=settings)
    return TemporalProjectRunResponse(success=True, project=project)


def import_temporal_override(
    request: TemporalOverrideRequest,
    *,
    settings: Settings,
) -> TemporalProjectRunResponse:
    project = _load_project(settings, request.project_id)
    if project.aoi_geojson is None:
        return TemporalProjectRunResponse(success=False, error_message="Project AOI is required before importing an override.", project=project)

    aoi_geometry = parse_aoi_geometry(project.aoi_geojson)
    override_geojson = _normalize_project_geometry(request.override_geojson, aoi_geometry)

    milestone = next((item for item in project.milestones if item.release_identifier == request.release_identifier), None)
    if milestone is None:
        return TemporalProjectRunResponse(
            success=False,
            error_message=f"Unknown milestone release: {request.release_identifier}",
            project=project,
        )

    milestone.manual_override_geojson = override_geojson
    project = _recompute_project_outputs(project, aoi_geometry)
    project.updated_at = _utc_now_iso()
    project = _refresh_project_bundle(project, settings)
    _save_project(project, settings)
    return TemporalProjectRunResponse(success=True, project=project)
