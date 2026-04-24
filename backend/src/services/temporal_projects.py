from __future__ import annotations

import csv
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import zipfile
from typing import Any, Callable

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.config import Settings
from src.domain.cache import load_cached_response
from src.domain.vectorize import build_temporal_growth_blocks, build_temporal_growth_envelope
from src.execution_profiles import PipelineExecutionConfig, resolve_backend
from src.schemas import (
    RunRequest,
    RunResponse,
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
from src.services.releases import list_releases
from src.services.validation import validate_request
from src.utils.geometry import geodesic_area_m2, normalized_aoi_geojson, parse_aoi_geometry


PairRunner = Callable[[RunRequest], RunResponse]


PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,128}$")
PROJECT_REGISTRY_FILENAME = "temporal_projects_registry.json"


@dataclass(frozen=True)
class TemporalMilestonePlanEntry:
    index: int
    release_identifier: str
    previous_release_identifier: str | None
    expected_request_hash: str | None
    cached_response: RunResponse | None
    reusable: bool
    blocking_errors: list[str]


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


def _populate_milestone_release_dates(project: TemporalProject, settings: Settings) -> TemporalProject:
    releases_by_id = {release.identifier: release for release in list_releases(settings)}
    for milestone in project.milestones:
        release = releases_by_id.get(milestone.release_identifier)
        if release is not None:
            milestone.release_date = str(release.release_date)
    return project


def _default_temporal_execution_config(settings: Settings) -> PipelineExecutionConfig:
    if settings.model_backend_default == "bandon_mps":
        return PipelineExecutionConfig(model_backend="bandon_mps")
    return PipelineExecutionConfig(model_backend="sam3", backend_mode="public_zerogpu")


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
            return PipelineExecutionConfig(model_backend="bandon_mps")

    if saw_legacy_pair:
        return PipelineExecutionConfig(model_backend="sam3", backend_mode="public_zerogpu")

    return _default_temporal_execution_config(settings)


def _project_dir(settings: Settings, project_id: str) -> Path:
    safe_id = _safe_project_id(project_id)
    path = settings.temporal_projects_dir / safe_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_registry_path(settings: Settings) -> Path:
    return settings.runtime_cache_dir / PROJECT_REGISTRY_FILENAME


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
    use_t1_preview: bool,
) -> TemporalReferenceImagery | None:
    if response is None or response.preview_images is None:
        return None

    preview_images = response.preview_images
    image_path = preview_images.t1_preview_path if use_t1_preview else preview_images.t2_preview_path
    image_png_data_url = (
        preview_images.t1_preview_png_data_url
        if use_t1_preview
        else preview_images.t2_preview_png_data_url
    )
    if image_png_data_url is None:
        image_png_data_url = _png_file_to_data_url(image_path)
    raster_bounds_wgs84 = preview_images.raster_bounds_wgs84

    if image_path is None and image_png_data_url is None:
        return None

    return TemporalReferenceImagery(
        image_path=image_path,
        image_png_data_url=image_png_data_url,
        raster_bounds_wgs84=raster_bounds_wgs84,
    )


def _hydrate_reference_imagery(project: TemporalProject, settings: Settings) -> TemporalProject:
    milestones = project.milestones
    for index, milestone in enumerate(milestones):
        reference_imagery: TemporalReferenceImagery | None = None

        if milestone.pair_request_hash:
            reference_imagery = _reference_imagery_from_pair_response(
                load_cached_response(settings, milestone.pair_request_hash),
                use_t1_preview=False,
            )

        if reference_imagery is None and index + 1 < len(milestones):
            next_pair_request_hash = milestones[index + 1].pair_request_hash
            if next_pair_request_hash:
                reference_imagery = _reference_imagery_from_pair_response(
                    load_cached_response(settings, next_pair_request_hash),
                    use_t1_preview=True,
                )

        milestone.reference_imagery = reference_imagery

    return project


def _hydrate_milestone_buffer_layers(project: TemporalProject, settings: Settings) -> TemporalProject:
    for milestone in project.milestones:
        if milestone.buffer_layers_geojson or not milestone.pair_request_hash:
            continue
        response = load_cached_response(settings, milestone.pair_request_hash)
        if response is not None:
            milestone.buffer_layers_geojson = response.buffer_layers_geojson
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

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
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
                ("cumulative_union.geojson", "Cumulative union up to this milestone", milestone.cumulative_union_geojson),
                ("cumulative_convex_hull.geojson", "Convex hull of cumulative union up to this milestone", milestone.cumulative_convex_hull_geojson),
                ("cumulative_growth_blocks.geojson", "Grouped blocks built from cumulative union", milestone.cumulative_growth_blocks_geojson),
                ("cumulative_growth_envelope.geojson", "Smoothed cumulative growth envelope", milestone.cumulative_growth_envelope_geojson),
            ):
                artifact_path = _artifact_path_for_milestone(project_dir, milestone.release_identifier, name)
                written_path = _write_geojson(artifact_path, payload)
                if written_path:
                    archive.write(artifact_path, arcname=f"milestones/{milestone.release_identifier}/{name}")
                    milestone_artifacts.append(
                        TemporalArtifactEntry(
                            name=f"{milestone.release_identifier}_{name.replace('.geojson', '')}",
                            path=written_path,
                            media_type="application/geo+json",
                            description=description,
                        )
                    )

            if milestone.pair_request_hash:
                pair_dir = settings.request_cache_dir / milestone.pair_request_hash
                pair_bundle = pair_dir / "export_bundle.zip"
                if pair_bundle.exists():
                    archive.write(pair_bundle, arcname=f"milestones/{milestone.release_identifier}/pair_export_bundle.zip")
                    milestone_artifacts.append(
                        TemporalArtifactEntry(
                            name=f"{milestone.release_identifier}_pair_export_bundle",
                            path=str(pair_bundle),
                            media_type="application/zip",
                            description="Underlying pairwise run export bundle",
                        )
                    )

            milestone.artifacts = milestone_artifacts

        manifest_payload = project.model_dump(mode="json")
        manifest_path.write_text(json.dumps(manifest_payload, indent=2))
        archive.write(manifest_path, arcname="project_manifest.json")

    project.download_bundle_path = str(bundle_path)
    return project


def _load_project(settings: Settings, project_id: str) -> TemporalProject:
    path = _project_json_path(settings, project_id)
    if not path.exists():
        raise FileNotFoundError(f"Unknown temporal project: {project_id}")
    project = TemporalProject.model_validate(json.loads(path.read_text()))
    project.execution_config = resolve_temporal_project_execution_config(project, settings)
    project = _populate_milestone_release_dates(project, settings)
    if project.project_dir is None:
        project.project_dir = str(path.parent)
    project = _sort_temporal_milestones(project)
    for milestone in project.milestones:
        if milestone.cumulative_convex_hull_geojson is None and milestone.cumulative_union_geojson is not None:
            milestone.cumulative_convex_hull_geojson = _feature_collection_from_convex_hull(
                _geometry_from_geojson(milestone.cumulative_union_geojson)
            )
    project = _hydrate_reference_imagery(project, settings)
    project = _hydrate_milestone_buffer_layers(project, settings)
    return _refresh_temporal_derived_geometry_layers(project)


def _save_project(project: TemporalProject, settings: Settings) -> TemporalProject:
    project = _populate_milestone_release_dates(project, settings)
    project = _sort_temporal_milestones(project)
    project.execution_config = resolve_temporal_project_execution_config(project, settings)
    project = _hydrate_reference_imagery(project, settings)
    project = _hydrate_milestone_buffer_layers(project, settings)
    project = _refresh_temporal_derived_geometry_layers(project)
    project.updated_at = _utc_now_iso()
    project_dir = _resolve_project_dir(settings, project.project_id, project.project_dir)
    project.project_dir = str(project_dir)
    registry = _load_project_registry(settings)
    registry[project.project_id] = str(project_dir)
    _save_project_registry(settings, registry)
    path = project_dir / "project.json"
    path.write_text(json.dumps(project.model_dump(mode="json"), indent=2))
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
):
    validation_request = ValidationRequest(
        aoi_geojson=aoi_geojson,
        t1_release=previous_release_identifier,
        t2_release=milestone_release_identifier,
        mode="full_run",
    )
    validation_response, prepared = validate_request(
        validation_request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=remote_patch_budget_enabled,
        request_hash_context=request_hash_context,
    )
    return validation_request, validation_response, prepared


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
    plan: list[TemporalMilestonePlanEntry] = []
    previous_release_id: str | None = None
    previous_successful_release_id: str | None = None

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


def _load_saved_project_summary(
    project_json_path: Path,
    *,
    expected_project_id: str | None = None,
) -> TemporalProjectSummary | None:
    try:
        project = TemporalProject.model_validate(json.loads(project_json_path.read_text()))
    except Exception:
        return None

    if expected_project_id is not None and project.project_id != expected_project_id:
        return None

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
        validation_request = ValidationRequest(
            aoi_geojson=project.aoi_geojson,
            t1_release=pair_source_release_id,
            t2_release=milestone.release_identifier,
            mode="full_run",
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
        return _load_project(settings, project_id)
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
    return _save_project(normalized, settings)


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
    resolved_execution_config = execution_config or resolve_temporal_project_execution_config(project, settings)
    if request_hash_context is None:
        backend = resolve_backend(resolved_execution_config, settings=settings)
        settings = backend.configure_settings(settings)
        remote_patch_budget_enabled = backend.enforce_remote_patch_budget()
        request_hash_context = backend.request_hash_context(settings)
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
        return TemporalProjectRunResponse(success=True, project=project)

    previous_successful_release_identifier = project.milestones[dirty_start - 1].release_identifier if dirty_start > 0 else None
    previous_cumulative = (
        GeometryCollection()
        if dirty_start == 0
        else _geometry_from_geojson(project.milestones[dirty_start - 1].cumulative_union_geojson)
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
