from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import csv
import base64
import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
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
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.config import Settings
from src.domain.cache import load_cached_response
from src.domain.imagery_providers import EsriWaybackProvider, MapboxCurrentProvider
from src.domain.mapbox_current import MAPBOX_SOURCE_ID
from src.domain.stage_timing import StageTimingRecorder
from src.domain.tiling import tile_bounds_3857, tile_range_for_bbox
from src.domain.vectorize import (
    VectorizationContext,
    build_change_buffer_layers,
    build_temporal_growth_blocks,
    build_temporal_growth_envelope,
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
)
from src.services.processing import ResolvedWaybackRelease, _resolve_release_for_aoi
from src.services.temporal_reference_imagery import TemporalReferenceSource, build_temporal_reference_imagery
from src.services.releases import list_releases
from src.services.validation import validate_request
from src.utils.geometry import bounds_dict, geodesic_area_m2, normalized_aoi_geojson, parse_aoi_geometry


PairRunner = Callable[[RunRequest], RunResponse]


PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,128}$")
PROJECT_REGISTRY_FILENAME = "temporal_projects_registry.json"
logger = logging.getLogger(__name__)
MAPBOX_CURRENT_RELEASE_DATE = "current_basemap"


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
    latest_source: str
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


def _is_mapbox_current_milestone(milestone: TemporalMilestone) -> bool:
    return milestone.release_identifier == MAPBOX_SOURCE_ID


def _mapbox_current_milestone() -> TemporalMilestone:
    return TemporalMilestone(
        release_identifier=MAPBOX_SOURCE_ID,
        release_date=MAPBOX_CURRENT_RELEASE_DATE,
        status="pending",
        source_mode="automated",
        warnings=["The latest milestone uses Mapbox Satellite current basemap imagery. Exact capture date is not guaranteed."],
    )


def _sync_latest_source_milestone(project: TemporalProject) -> TemporalProject:
    mapbox_milestones = [milestone for milestone in project.milestones if _is_mapbox_current_milestone(milestone)]
    wayback_milestones = [milestone for milestone in project.milestones if not _is_mapbox_current_milestone(milestone)]

    if project.latest_source != "mapbox_current":
        project.milestones = wayback_milestones
        return project

    if not wayback_milestones:
        project.milestones = []
        return project

    mapbox_milestone = mapbox_milestones[-1] if mapbox_milestones else _mapbox_current_milestone()
    mapbox_milestone.release_date = MAPBOX_CURRENT_RELEASE_DATE
    project.milestones = [*wayback_milestones, mapbox_milestone]
    return project


def _populate_milestone_release_dates(project: TemporalProject, settings: Settings) -> TemporalProject:
    if project.milestones and all(milestone.release_date for milestone in project.milestones):
        return project

    releases_by_id = {release.identifier: release for release in list_releases(settings)}
    for milestone in project.milestones:
        if _is_mapbox_current_milestone(milestone):
            milestone.release_date = MAPBOX_CURRENT_RELEASE_DATE
            continue
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
    milestones = payload.get("milestones")
    if isinstance(milestones, list):
        milestones.sort(key=lambda item: str(item.get("release_date") or "") if isinstance(item, dict) else "")
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
    normalized = _normalize_project_dir(project_dir)
    if normalized is not None:
        existing_project_path = normalized / "project.json"
        if existing_project_path.exists():
            try:
                existing_payload = json.loads(existing_project_path.read_text())
            except Exception:
                existing_payload = None
            existing_project_id = existing_payload.get("project_id") if isinstance(existing_payload, dict) else None
            if isinstance(existing_project_id, str) and existing_project_id and existing_project_id != project_id:
                normalized = normalized / _safe_project_id(project_id)
        normalized.mkdir(parents=True, exist_ok=True)
        return normalized

    registry = _load_project_registry(settings)
    registered_dir = registry.get(project_id)
    if registered_dir:
        path = Path(registered_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _project_dir(settings, project_id)


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


def _feature_collection_from_convex_hull(geometry: BaseGeometry) -> dict[str, Any]:
    return _feature_collection_from_geometry(geometry.convex_hull)


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


def _write_geojson(path: Path, payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    path.write_text(json.dumps(payload, indent=2))
    return str(path)


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
) -> TemporalReferenceImagery | None:
    if response is None or response.preview_images is None:
        return None

    preview_images = response.preview_images
    image_path = preview_images.t1_preview_path if use_t1_preview else preview_images.t2_preview_path
    image_png_data_url = None
    if include_data_url:
        image_png_data_url = (
            preview_images.t1_preview_png_data_url
            if use_t1_preview
            else preview_images.t2_preview_png_data_url
        )
    if include_data_url and image_png_data_url is None:
        image_png_data_url = _png_file_to_data_url(image_path)
    raster_bounds_wgs84 = preview_images.raster_bounds_wgs84

    source_raster_path = _reference_source_raster_path_from_pair_response(response, use_t1_preview=use_t1_preview)
    valid_mask_path = _reference_valid_mask_path_from_pair_response(response, use_t1_preview=use_t1_preview)

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
    )


def _reference_source_raster_path_from_pair_response(
    response: RunResponse | None,
    *,
    use_t1_preview: bool,
) -> str | None:
    if response is None:
        return None

    artifact_name = "t1_wayback_rgb_tif" if use_t1_preview else "t2_wayback_rgb_tif"
    for artifact in response.artifacts:
        if artifact.name == artifact_name:
            return artifact.path

    preview_images = response.preview_images
    if preview_images is None:
        return None
    preview_path = preview_images.t1_preview_path if use_t1_preview else preview_images.t2_preview_path
    if not preview_path:
        return None
    request_dir = Path(preview_path).expanduser().resolve().parent
    fallback_name = "t1_wayback_rgb.tif" if use_t1_preview else "t2_wayback_rgb.tif"
    fallback_path = request_dir / fallback_name
    if fallback_path.is_file():
        return str(fallback_path)
    return None


def _reference_valid_mask_path_from_pair_response(
    response: RunResponse | None,
    *,
    use_t1_preview: bool,
) -> str | None:
    if response is None or response.preview_images is None:
        return None
    preview_path = response.preview_images.t1_preview_path if use_t1_preview else response.preview_images.t2_preview_path
    if not preview_path:
        return None
    request_dir = Path(preview_path).expanduser().resolve().parent
    pattern = "t1_*_valid_mask.tif" if use_t1_preview else "t2_*_valid_mask.tif"
    matches = sorted(request_dir.glob(pattern))
    if matches:
        return str(matches[0])
    return None


def _temporal_reference_aoi_hash(aoi_geojson: dict[str, Any] | None) -> str:
    if not aoi_geojson:
        return "no-aoi"
    normalized = normalized_aoi_geojson(aoi_geojson)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _temporal_reference_route(project_id: str, release_identifier: str, suffix: str) -> str:
    return f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/{suffix}"


def _reference_imagery_from_cog_path(
    *,
    project_id: str,
    release_identifier: str,
    cog_path: Path,
    source_reference: TemporalReferenceImagery | None = None,
) -> TemporalReferenceImagery:
    return TemporalReferenceImagery(
        image_path=source_reference.image_path if source_reference else None,
        image_png_data_url=None,
        raster_bounds_wgs84=source_reference.raster_bounds_wgs84 if source_reference else None,
        storage_strategy="raster_tiles",
        cog_path=str(cog_path),
        cog_url=f"/api/files?path={quote(str(cog_path))}",
        tilejson_url=_temporal_reference_route(project_id, release_identifier, "tilejson.json"),
        tiles_url_template=_temporal_reference_route(project_id, release_identifier, "tiles/{z}/{x}/{y}.png"),
        minzoom=source_reference.minzoom if source_reference else None,
        maxzoom=source_reference.maxzoom if source_reference else None,
        tile_size=source_reference.tile_size if source_reference and source_reference.tile_size else 256,
    )


def _link_or_copy_reference_cog(source_path: Path, target_path: Path) -> str:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        return "existing"
    try:
        target_path.hardlink_to(source_path)
        return "linked"
    except OSError:
        shutil.copy2(source_path, target_path)
        return "copied"


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
            payload = json.loads(project_json_path.read_text())
            candidate = TemporalProject.model_validate(payload)
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
    payload = project.model_dump(mode="json")
    project_json_path = project_dir / "project.json"
    project_json_path.write_text(json.dumps(payload, indent=2))
    manifest_path = project_dir / "project_manifest.json"
    if manifest_path.exists():
        manifest_path.write_text(json.dumps(payload, indent=2))
    _write_project_summary(project, project_json_path)


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
            milestone.reference_imagery = _reference_imagery_from_cog_path(
                project_id=project.project_id,
                release_identifier=release_identifier,
                cog_path=target_cog_path,
                source_reference=milestone.reference_imagery,
            )
            repaired_count += 1
            logger.info(
                "REFERENCE_REUSE_COG_LINKED projectId=%s releaseIdentifier=%s aoiHash=%s sourcePath=%s targetPath=%s reason=project_local_cog",
                project.project_id,
                release_identifier,
                aoi_hash,
                target_cog_path,
                target_cog_path,
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
            action = _link_or_copy_reference_cog(source_path, target_cog_path)
            milestone.reference_imagery = _reference_imagery_from_cog_path(
                project_id=project.project_id,
                release_identifier=release_identifier,
                cog_path=target_cog_path,
                source_reference=source_reference,
            )
            repaired_count += 1
            logger.info(
                "REFERENCE_REUSE_COG_%s projectId=%s releaseIdentifier=%s aoiHash=%s sourceProjectId=%s sourcePath=%s targetPath=%s durationMs=%s",
                "LINKED" if action == "linked" else "COPIED",
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
            )
            if reference and reference.cog_path:
                milestone.reference_imagery = reference
                repaired_count += 1
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
        and milestone.cumulative_growth_envelope_geojson is not None
        and milestone.metrics is not None
    )


def _ensure_temporal_derived_geometry_layers(project: TemporalProject) -> TemporalProject:
    if all(_milestone_has_derived_geometry_layers(milestone) for milestone in project.milestones):
        return project
    return _refresh_temporal_derived_geometry_layers(project)


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
            continue

        additions = milestone.additions_geojson
        if not _has_features(additions):
            logger.info(
                "TEMPORAL_OUTPUT_ARTIFACT_MISSING projectId=%s releaseIdentifier=%s artifactKey=building_change_buffer reason=%s",
                project.project_id,
                milestone.release_identifier,
                "unsupported_for_baseline" if milestone == project.milestones[0] else "empty_geojson",
            )
            continue

        previous_index = max(project.milestones.index(milestone) - 1, 0)
        previous_milestone = project.milestones[previous_index] if project.milestones else milestone
        try:
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
        "TEMPORAL_OUTPUT_LAYER_AVAILABILITY_BUILT projectId=%s milestoneCount=%s",
        project.project_id,
        len(project.milestones),
    )
    return project


def _refresh_temporal_derived_geometry_layers(project: TemporalProject) -> TemporalProject:
    if project.aoi_geojson is None:
        return project

    for milestone in project.milestones:
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
            _, milestone.cumulative_growth_envelope_geojson = build_temporal_growth_envelope(
                milestone.cumulative_union_geojson,
                aoi_geojson=project.aoi_geojson,
                release_identifier=milestone.release_identifier,
                release_date=release_date,
            )
        additions_geometry = _geometry_from_geojson(milestone.additions_geojson)
        effective_geometry = _geometry_from_geojson(milestone.cumulative_union_geojson)
        if not additions_geometry.is_empty or not effective_geometry.is_empty:
            milestone.metrics = _build_metrics(
                additions_geometry,
                effective_geometry,
                building_level_available=milestone.manual_override_geojson is None,
                effective_building_blocks_geojson=milestone.effective_building_blocks_geojson,
                cumulative_growth_blocks_geojson=milestone.cumulative_growth_blocks_geojson,
                cumulative_growth_envelope_geojson=milestone.cumulative_growth_envelope_geojson,
            )
    return project


def _refresh_project_bundle(project: TemporalProject, settings: Settings) -> TemporalProject:
    project = _hydrate_reference_imagery(project, settings)
    project = _hydrate_milestone_buffer_layers(project, settings)
    project = _refresh_temporal_derived_geometry_layers(project)
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    bundle_path = project_dir / "temporal_project_bundle.zip"
    manifest_path = project_dir / "project_manifest.json"

    for milestone in project.milestones:
        milestone_artifacts: list[TemporalArtifactEntry] = []
        for name, description, payload in (
            ("automated_additions.geojson", "Automated additions footprint", milestone.automated_additions_geojson),
            ("automated_candidate_footprint.geojson", "Automated cumulative candidate footprint", milestone.automated_candidate_footprint_geojson),
            ("automated_building_blocks.geojson", "Automated building-level blocks", milestone.automated_building_blocks_geojson),
            ("manual_override.geojson", "Manual milestone override", milestone.manual_override_geojson),
            ("additions.geojson", "Effective additions since previous milestone", milestone.additions_geojson),
            ("effective_building_blocks.geojson", "Grouped blocks built from effective additions", milestone.effective_building_blocks_geojson),
            ("effective_footprint.geojson", "Effective footprint at this milestone", milestone.effective_footprint_geojson),
            ("building_change_buffer_10m.geojson", "Building-change buffer 10 m", milestone.buffer_layers_geojson.get("10m")),
            ("building_change_buffer_15m.geojson", "Building-change buffer 15 m", milestone.buffer_layers_geojson.get("15m")),
            ("building_change_buffer_20m.geojson", "Building-change buffer 20 m", milestone.buffer_layers_geojson.get("20m")),
            ("cumulative_union.geojson", "Cumulative union up to this milestone", milestone.cumulative_union_geojson),
            ("cumulative_convex_hull.geojson", "Convex hull of cumulative union up to this milestone", milestone.cumulative_convex_hull_geojson),
            ("cumulative_growth_blocks.geojson", "Grouped blocks built from cumulative union", milestone.cumulative_growth_blocks_geojson),
            ("cumulative_growth_envelope.geojson", "Smoothed cumulative growth envelope", milestone.cumulative_growth_envelope_geojson),
        ):
            artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, name)
            written_path = _write_geojson(artifact_path, payload)
            if written_path:
                milestone_artifacts.append(
                    TemporalArtifactEntry(
                        name=f"{milestone.release_identifier}_{name.replace('.geojson', '')}",
                        path=written_path,
                        media_type="application/geo+json",
                        description=description,
                    )
                )

        milestone.artifacts = milestone_artifacts

    manifest_payload = project.model_dump(mode="json")
    manifest_path.write_text(json.dumps(manifest_payload, indent=2))
    project.download_bundle_path = str(bundle_path) if bundle_path.exists() else None
    return project


def _safe_export_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned or "projet_temporel"


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value or value == MAPBOX_CURRENT_RELEASE_DATE:
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
    if _is_mapbox_current_milestone(milestone):
        ym = export_now.strftime("%Y-%m")
        return ym, f"actuel {ym}"

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


def _validate_temporal_qgis_export(
    *,
    export_build_dir: Path,
    qgz_path: Path,
    gpkg_path: Path,
    expected_gpkg_layers: set[str],
    raster_paths: list[Path],
) -> None:
    if not qgz_path.exists():
        raise ValueError("QGIS export missing .qgz project.")
    if not gpkg_path.exists():
        raise ValueError("QGIS export missing .gpkg dataset.")

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

    datasource = ogr.Open(str(gpkg_path), 0)
    if datasource is None:
        raise ValueError(f"Unable to open exported GeoPackage: {gpkg_path}")
    actual_layers = {datasource.GetLayerByIndex(index).GetName() for index in range(datasource.GetLayerCount())}
    datasource = None
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
    project = _load_project(settings, project_id)
    project = _refresh_project_bundle(project, settings)
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
        ("cumulative_union_geojson", "union_cumulative", "Union cumulative"),
        ("cumulative_convex_hull_geojson", "polygone_convexe", "Polygone convexe"),
        ("cumulative_growth_envelope_geojson", "polygone_concave", "Polygone concave"),
    ]

    try:
        for milestone_index, (milestone, year_month, label, pair_dir) in enumerate(milestone_context):
            date_prefix = year_month.replace("-", "_")
            milestone_slug = "mapbox_actuel" if _is_mapbox_current_milestone(milestone) else milestone.release_identifier
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
                    continue

                gpkg_layer_name = f"{layer_stub}_{date_prefix}"
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
        gpkg_dataset = None

    manifest_path = export_build_dir / "manifeste_projet.json"
    qgz_name = f"{safe_name}_{start_ym}_{end_ym}.qgz"
    qgz_path = qgis_dir / qgz_name
    internal_qgs_name = _write_qgz_project(
        qgz_path,
        project_name=project.name,
        layer_groups=qgis_groups,
    )

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
                "notes": [
                    "L’imagerie Mapbox actuelle est nommée avec la date d’export (YYYY-MM), car la date exacte d’acquisition de l’image satellite n’est pas garantie."
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    _validate_temporal_qgis_export(
        export_build_dir=export_build_dir,
        qgz_path=qgz_path,
        gpkg_path=gpkg_path,
        expected_gpkg_layers=set(gpkg_layer_names),
        raster_paths=raster_output_paths,
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
    project = TemporalProject.model_validate(payload)
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
    if refresh_derived_layers:
        for milestone in project.milestones:
            if milestone.cumulative_convex_hull_geojson is None and milestone.cumulative_union_geojson is not None:
                milestone.cumulative_convex_hull_geojson = _feature_collection_from_convex_hull(
                    _geometry_from_geojson(milestone.cumulative_union_geojson)
                )
    if hydrate_reference_imagery:
        project = _hydrate_reference_imagery(project, settings)
    if hydrate_buffer_layers:
        project = _hydrate_milestone_buffer_layers(project, settings)
    if refresh_derived_layers:
        project = _ensure_temporal_derived_geometry_layers(project)
    logger.info(
        "PROJECT_LOAD_TIMING projectId=%s phase=layer_availability ms=%s",
        project_id,
        round((time.perf_counter() - layer_availability_started_at) * 1000, 2),
    )
    final_project_payload = json.dumps(project.model_dump(mode="json"), sort_keys=True) if write_side_effects else None
    if write_side_effects and (should_compact_project_json or stripped_fields or final_project_payload != initial_project_payload):
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
    project = _hydrate_milestone_buffer_layers(project, settings)
    project = _refresh_temporal_derived_geometry_layers(project)
    _strip_redundant_reference_imagery_data_urls(project)
    project.updated_at = _utc_now_iso()
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    project.project_dir = str(project_dir)
    registry = _load_project_registry(settings)
    registry[project.project_id] = str(project_dir)
    _save_project_registry(settings, registry)
    path = project_dir / "project.json"
    path.write_text(json.dumps(project.model_dump(mode="json"), indent=2))
    _write_project_summary(project, path)
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
    latest_source: str = "esri_wayback",
    existing_footprint_geojson: dict[str, Any] | None = None,
):
    validation_request = ValidationRequest(
        aoi_geojson=aoi_geojson,
        t1_release=previous_release_identifier,
        t2_release=milestone_release_identifier,
        mode="full_run",
        latest_source=latest_source,  # type: ignore[arg-type]
        existing_footprint_geojson=existing_footprint_geojson,
    )
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
    latest_wayback_release = max(releases, key=lambda item: item.release_date) if releases else None
    prefetch_plans: list[TemporalImageryPrefetchPlan] = []
    for entry in pair_plan:
        if entry.index == 0 or entry.reusable or entry.blocking_errors or entry.expected_request_hash is None:
            continue
        milestone = project.milestones[entry.index]
        is_mapbox_current = _is_mapbox_current_milestone(milestone)
        if is_mapbox_current and latest_wayback_release is None:
            continue
        prefetch_plans.append(
            TemporalImageryPrefetchPlan(
                pair_index=entry.index,
                request_hash=entry.expected_request_hash,
                t1_provider="esri_wayback",
                t2_provider="mapbox" if is_mapbox_current else "esri_wayback",
                t1_release_identifier=entry.previous_release_identifier or milestone.release_identifier,
                t2_release_identifier=milestone.release_identifier,
                latest_source="mapbox_current" if is_mapbox_current else "esri_wayback",
                aoi_geojson=project.aoi_geojson,
                t2_effective_release_identifier=latest_wayback_release.identifier if is_mapbox_current else milestone.release_identifier,
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

        if plan.t2_provider == "mapbox":
            scene_t2 = MapboxCurrentProvider().download(
                aoi_bbox,
                settings=derived_settings,
                zoom=min(derived_settings.mapbox_current_imagery_default_zoom, derived_settings.mapbox_current_imagery_max_zoom),
                aoi_geojson=plan.aoi_geojson,
            )
            metadata.update(
                {
                    "t2_cache_key": scene_t2.cache_key,
                    "t2_zoom": scene_t2.zoom,
                    "t2_source_id": scene_t2.source_id,
                    "t2_cache_hit": bool((scene_t2.metadata or {}).get("cache_hit")),
                }
            )
        else:
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
        is_mapbox_current = _is_mapbox_current_milestone(milestone)
        milestone_release_identifier = (
            latest_wayback_release.identifier
            if is_mapbox_current and latest_wayback_release is not None
            else milestone.release_identifier
        )
        _, validation_response, prepared = _prepare_temporal_pair_request(
            aoi_geojson=project.aoi_geojson,
            previous_release_identifier=previous_identifier,
            milestone_release_identifier=milestone_release_identifier,
            releases=releases,
            settings=settings,
            remote_patch_budget_enabled=remote_patch_budget_enabled,
            request_hash_context=request_hash_context,
            latest_source="mapbox_current" if is_mapbox_current and index == last_index else "esri_wayback",
            existing_footprint_geojson=_feature_collection_from_geometry(previous_cumulative),
        )
        expected_request_hash = prepared.request_hash if prepared is not None else None
        cached_response = (
            _load_cached_run_response(settings, expected_request_hash)
            if expected_request_hash is not None
            else None
        )
        reusable = (
            not validation_response.blocking_errors
            and milestone.status == "complete"
            and expected_request_hash is not None
            and milestone.pair_request_hash == expected_request_hash
            and cached_response is not None
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
) -> None:
    automated_additions_geojson = (
        response.new_buildings_geojson
        or response.change_polygons_geojson
        or _empty_feature_collection()
    )
    automated_additions_geometry = _geometry_from_geojson(automated_additions_geojson).intersection(aoi_geometry).buffer(0)
    automated_candidate_geometry = unary_union([previous_cumulative, automated_additions_geometry]).intersection(aoi_geometry).buffer(0)

    milestone.pair_request_hash = request_hash or (response.summary.request_hash if response.summary is not None else None)
    milestone.automated_additions_geojson = automated_additions_geojson
    milestone.automated_candidate_footprint_geojson = _feature_collection_from_geometry(automated_candidate_geometry)
    milestone.automated_building_blocks_geojson = response.building_blocks_geojson or _empty_feature_collection()
    milestone.buffer_layers_geojson = response.buffer_layers_geojson
    milestone.warnings = [
        warning
        for warning in ((response.diagnostics.warnings if response.diagnostics else []) or [])
        if isinstance(warning, str)
    ]


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
    buffer_envelope_geojson = _buffer_layer_geojson(response.buffer_layers_geojson) or _empty_feature_collection()
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
        growth_envelope_area_m2=round(float(total_area_m2 or 0.0), 2),
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
        cumulative_convex_hull_geojson=_empty_feature_collection(),
        cumulative_growth_blocks_geojson=_empty_feature_collection(),
        cumulative_growth_envelope_geojson=_empty_feature_collection(),
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
        cumulative_convex_hull_geojson=_feature_collection_from_convex_hull(_geometry_from_geojson(changes_geojson)),
        cumulative_growth_blocks_geojson=building_blocks_geojson,
        cumulative_growth_envelope_geojson=buffer_envelope_geojson,
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
        project = TemporalProject.model_validate(json.loads(project_json_path.read_text()))
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
    latest_wayback_release = max(releases, key=lambda item: item.release_date) if releases else None
    seen: set[str] = set()
    previous_release_id: str | None = None
    previous_successful_release_id: str | None = None
    previous_release_date = None

    last_index = len(project.milestones) - 1
    for index, milestone in enumerate(project.milestones):
        is_mapbox_current = _is_mapbox_current_milestone(milestone)
        release = latest_wayback_release if is_mapbox_current else releases_by_id.get(milestone.release_identifier)
        if release is None:
            blocking_errors.append(f"Unknown Wayback release: {milestone.release_identifier}")
            continue

        milestone.release_date = MAPBOX_CURRENT_RELEASE_DATE if is_mapbox_current else str(release.release_date)
        if milestone.release_identifier in seen:
            blocking_errors.append(f"Duplicate milestone release: {milestone.release_identifier}")
        seen.add(milestone.release_identifier)

        if not is_mapbox_current and previous_release_date is not None and release.release_date <= previous_release_date:
            blocking_errors.append("Milestones must be in strictly chronological order.")
        if not is_mapbox_current:
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
        validation_request = ValidationRequest(
            aoi_geojson=project.aoi_geojson,
            t1_release=pair_source_release_id,
            t2_release=release.identifier,
            mode="full_run",
            latest_source="mapbox_current" if is_mapbox_current and index == last_index else "esri_wayback",
        )
        validation_response, _ = validate_request(
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
        milestone.status = "validated" if not validation_response.blocking_errors else "error"
        if not validation_response.blocking_errors:
            previous_successful_release_id = milestone.release_identifier
        previous_release_id = milestone.release_identifier

    return pair_estimates, warnings, blocking_errors


def list_temporal_projects(settings: Settings, *, include_cached_runs: bool = False) -> list[TemporalProjectSummary]:
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
    normalized = _sync_latest_source_milestone(normalized)
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
    normalized = _sync_latest_source_milestone(normalized)
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
            milestone.cumulative_convex_hull_geojson = None
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
        milestone.cumulative_convex_hull_geojson = _feature_collection_from_convex_hull(effective_geometry)

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
            _, cumulative_growth_envelope_geojson = build_temporal_growth_envelope(
                milestone.cumulative_union_geojson,
                aoi_geojson=project.aoi_geojson,
                release_identifier=milestone.release_identifier,
                release_date=release_date,
            )
            milestone.effective_building_blocks_geojson = effective_blocks_geojson
            milestone.cumulative_growth_blocks_geojson = cumulative_blocks_geojson
            milestone.cumulative_growth_envelope_geojson = cumulative_growth_envelope_geojson
        else:
            milestone.effective_building_blocks_geojson = _empty_feature_collection()
            milestone.cumulative_growth_blocks_geojson = _empty_feature_collection()
            milestone.cumulative_growth_envelope_geojson = _empty_feature_collection()

        milestone.metrics = _build_metrics(
            additions_geometry,
            effective_geometry,
            building_level_available=manual_geometry.is_empty,
            effective_building_blocks_geojson=milestone.effective_building_blocks_geojson,
            cumulative_growth_blocks_geojson=milestone.cumulative_growth_blocks_geojson,
            cumulative_growth_envelope_geojson=milestone.cumulative_growth_envelope_geojson,
        )
        if milestone.status != "error":
            milestone.status = "complete"
            milestone.error_message = None
        previous_cumulative = effective_geometry

    return project


def _recompute_project_outputs(project: TemporalProject, aoi_geometry: BaseGeometry) -> TemporalProject:
    return _recompute_project_outputs_from_index(project, aoi_geometry, 0)


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
        metadata={
            "milestone_count": len(project.milestones),
            "latest_source": project.latest_source,
        },
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
    project = _recompute_project_outputs_from_index(project, aoi_geometry, 0, 0)
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
        return TemporalProjectRunResponse(success=True, project=project)

    previous_successful_release_identifier = project.milestones[dirty_start - 1].release_identifier if dirty_start > 0 else None
    previous_cumulative = (
        GeometryCollection()
        if dirty_start == 0
        else _geometry_from_geojson(project.milestones[dirty_start - 1].cumulative_union_geojson)
    )

    releases = list_releases(settings)
    latest_wayback_release = max(releases, key=lambda item: item.release_date) if releases else None
    for index in range(dirty_start, len(project.milestones)):
        milestone = project.milestones[index]
        milestone.warnings = []
        milestone.error_message = None

        previous_release_identifier = previous_successful_release_identifier or project.milestones[index - 1].release_identifier
        is_mapbox_current = _is_mapbox_current_milestone(milestone)
        milestone_release_identifier = (
            latest_wayback_release.identifier
            if is_mapbox_current and latest_wayback_release is not None
            else milestone.release_identifier
        )
        run_request, validation_response, prepared = _prepare_temporal_pair_request(
            aoi_geojson=project.aoi_geojson,
            previous_release_identifier=previous_release_identifier,
            milestone_release_identifier=milestone_release_identifier,
            releases=releases,
            settings=settings,
            remote_patch_budget_enabled=remote_patch_budget_enabled,
            request_hash_context=request_hash_context,
            latest_source="mapbox_current" if is_mapbox_current and index == len(project.milestones) - 1 else "esri_wayback",
            existing_footprint_geojson=_feature_collection_from_geometry(previous_cumulative),
        )
        if prepared is None or validation_response.blocking_errors:
            milestone.status = "error"
            milestone.error_message = "; ".join(validation_response.blocking_errors) or "Temporal pair validation failed."
            project.updated_at = _utc_now_iso()
            continue

        cached_response = _load_cached_run_response(settings, prepared.request_hash)
        response = cached_response if cached_response is not None else pair_runner(
            RunRequest(
                aoi_geojson=run_request.aoi_geojson,
                t1_release=run_request.t1_release,
                t2_release=run_request.t2_release,
                mode=run_request.mode,
                latest_source=run_request.latest_source,
                existing_footprint_geojson=run_request.existing_footprint_geojson,
            )
        )
        if response is None or not response.success:
            milestone.status = "error"
            milestone.error_message = (response.error_message if response is not None else None) or "Temporal pair run failed."
            project.updated_at = _utc_now_iso()
            continue

        _apply_pair_response_to_milestone(
            milestone,
            response=response,
            previous_cumulative=previous_cumulative,
            aoi_geometry=aoi_geometry,
            request_hash=prepared.request_hash,
        )
        project = _recompute_project_outputs_from_index(project, aoi_geometry, index, index)
        previous_cumulative = _geometry_from_geojson(milestone.cumulative_union_geojson)
        previous_successful_release_identifier = milestone.release_identifier

    project.updated_at = _utc_now_iso()
    project = _refresh_project_bundle(project, settings)
    _save_project(project, settings)
    _write_temporal_project_timing_safely(timing, project)
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
