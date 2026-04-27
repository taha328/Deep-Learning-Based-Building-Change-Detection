from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.config import Settings
from src.db.geometry import geojson_to_wkt_element, polygonal_geojson_to_geometry
from src.db.models import ArtifactRecord, GeometryLayerRecord, MilestoneMetricRecord, MilestoneRecord, ProjectRecord
from src.db.session import session_scope
from src.repositories.artifact_repository import artifact_record_from_entry
from src.schemas import TemporalMilestone, TemporalMilestoneMetrics, TemporalProject, TemporalProjectSummary
from src.utils.geometry import geodesic_area_m2


GEOMETRY_LAYER_KINDS = {
    "aoi",
    "manual_override",
    "automated_additions",
    "automated_candidate_footprint",
    "automated_building_blocks",
    "additions",
    "effective_building_blocks",
    "effective_footprint",
    "cumulative_union",
    "cumulative_convex_hull",
    "cumulative_growth_blocks",
    "cumulative_growth_envelope",
    "buffer_10m",
    "buffer_15m",
    "buffer_20m",
}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _feature_count(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    if payload.get("type") == "FeatureCollection" and isinstance(payload.get("features"), list):
        return len(payload["features"])
    if payload.get("type") == "Feature":
        return 1
    return None


def _area_m2(payload: dict[str, Any] | None) -> float | None:
    geometry = polygonal_geojson_to_geometry(payload)
    if geometry is None or geometry.is_empty:
        return None
    try:
        return float(geodesic_area_m2(geometry))
    except Exception:
        return None


def _summary_from_project(project: TemporalProject) -> TemporalProjectSummary:
    complete = sum(1 for milestone in project.milestones if milestone.status == "complete")
    return TemporalProjectSummary(
        project_id=project.project_id,
        name=project.name,
        project_dir=project.project_dir,
        project_kind="temporal",
        display_name=project.name,
        semantics=project.semantics,
        milestone_count=len(project.milestones),
        complete_milestone_count=complete,
        created_at=project.created_at,
        updated_at=project.updated_at,
        download_bundle_path=project.download_bundle_path,
    )


def _metric_record(milestone_record: MilestoneRecord, metrics: TemporalMilestoneMetrics) -> MilestoneMetricRecord:
    payload = metrics.model_dump(mode="json")
    return MilestoneMetricRecord(
        milestone_id=milestone_record.id,
        added_area_m2=metrics.added_area_m2,
        total_area_m2=metrics.total_area_m2,
        additions_feature_count=metrics.additions_feature_count,
        effective_feature_count=metrics.effective_feature_count,
        building_level_available=metrics.building_level_available,
        added_block_count=metrics.added_block_count,
        cumulative_block_count=metrics.cumulative_block_count,
        added_block_area_m2=metrics.added_block_area_m2,
        cumulative_block_area_m2=metrics.cumulative_block_area_m2,
        growth_envelope_area_m2=metrics.growth_envelope_area_m2,
        raw_payload=payload,
    )


def _add_geometry_layer(
    session: Session,
    *,
    project: ProjectRecord,
    milestone: MilestoneRecord | None,
    layer_kind: str,
    geojson: dict[str, Any] | None,
    source: str | None = None,
) -> None:
    if not geojson or layer_kind not in GEOMETRY_LAYER_KINDS:
        return
    session.add(
        GeometryLayerRecord(
            project_db_id=project.id,
            milestone_id=milestone.id if milestone else None,
            layer_kind=layer_kind,
            geom=geojson_to_wkt_element(geojson),
            geojson=geojson,
            feature_count=_feature_count(geojson),
            area_m2=_area_m2(geojson),
            source=source,
        )
    )


def _milestone_geojson_layers(milestone: TemporalMilestone) -> list[tuple[str, dict[str, Any] | None]]:
    layers: list[tuple[str, dict[str, Any] | None]] = [
        ("manual_override", milestone.manual_override_geojson),
        ("automated_additions", milestone.automated_additions_geojson),
        ("automated_candidate_footprint", milestone.automated_candidate_footprint_geojson),
        ("automated_building_blocks", milestone.automated_building_blocks_geojson),
        ("additions", milestone.additions_geojson),
        ("effective_building_blocks", milestone.effective_building_blocks_geojson),
        ("effective_footprint", milestone.effective_footprint_geojson),
        ("cumulative_union", milestone.cumulative_union_geojson),
        ("cumulative_convex_hull", milestone.cumulative_convex_hull_geojson),
        ("cumulative_growth_blocks", milestone.cumulative_growth_blocks_geojson),
        ("cumulative_growth_envelope", milestone.cumulative_growth_envelope_geojson),
    ]
    for distance, payload in milestone.buffer_layers_geojson.items():
        normalized_distance = str(distance).replace(".", "_").replace("m", "")
        kind = f"buffer_{normalized_distance}m"
        layers.append((kind, payload))
    return layers


def save_project(
    project: TemporalProject,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> TemporalProject:
    if session is None:
        with session_scope(settings) as scoped_session:
            return save_project(project, settings=settings, session=scoped_session)

    payload = project.model_dump(mode="json")
    record = session.query(ProjectRecord).filter(ProjectRecord.project_id == project.project_id).one_or_none()
    if record is None:
        record = ProjectRecord(project_id=project.project_id, name=project.name, semantics=project.semantics)
        session.add(record)
        session.flush()

    record.name = project.name
    record.semantics = project.semantics
    record.project_dir = project.project_dir
    record.aoi_geojson = project.aoi_geojson
    record.aoi_geom = geojson_to_wkt_element(project.aoi_geojson)
    record.raw_payload = payload
    record.created_at = _parse_datetime(project.created_at) or record.created_at
    record.updated_at = _parse_datetime(project.updated_at) or datetime.now(UTC)

    session.query(GeometryLayerRecord).filter(GeometryLayerRecord.project_db_id == record.id).delete()
    session.query(ArtifactRecord).filter(ArtifactRecord.project_db_id == record.id).delete()
    session.query(MilestoneRecord).filter(MilestoneRecord.project_db_id == record.id).delete()
    session.flush()

    _add_geometry_layer(session, project=record, milestone=None, layer_kind="aoi", geojson=project.aoi_geojson, source="project")

    for milestone in project.milestones:
        milestone_record = MilestoneRecord(
            project_db_id=record.id,
            release_identifier=milestone.release_identifier,
            release_date=_parse_datetime(milestone.release_date),
            status=milestone.status,
            source_mode=milestone.source_mode,
            pair_request_hash=milestone.pair_request_hash,
            error_message=milestone.error_message,
            raw_payload=milestone.model_dump(mode="json"),
        )
        session.add(milestone_record)
        session.flush()

        if milestone.metrics is not None:
            session.add(_metric_record(milestone_record, milestone.metrics))

        for entry in milestone.artifacts:
            session.add(artifact_record_from_entry(entry, project=record, milestone=milestone_record))

        for layer_kind, geojson in _milestone_geojson_layers(milestone):
            _add_geometry_layer(
                session,
                project=record,
                milestone=milestone_record,
                layer_kind=layer_kind,
                geojson=geojson,
                source=milestone.source_mode,
            )

    if project.download_bundle_path:
        session.add(
            ArtifactRecord(
                project_db_id=record.id,
                name=f"{project.project_id}_temporal_project_bundle",
                path=project.download_bundle_path,
                media_type="application/zip",
                description="Temporal project export bundle",
                artifact_kind="bundle",
            )
        )
    return project


def get_project(project_id: str, *, settings: Settings | None = None, session: Session | None = None) -> TemporalProject:
    if session is None:
        with session_scope(settings) as scoped_session:
            return get_project(project_id, settings=settings, session=scoped_session)

    record = session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()
    if record is None or not record.raw_payload:
        raise FileNotFoundError(f"Unknown temporal project: {project_id}")
    return TemporalProject.model_validate(record.raw_payload)


def list_projects(
    *,
    settings: Settings | None = None,
    session: Session | None = None,
    include_cached_runs: bool = False,
) -> list[TemporalProjectSummary]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_projects(settings=settings, session=scoped_session, include_cached_runs=include_cached_runs)

    summaries: list[TemporalProjectSummary] = []
    for record in session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all():
        if not record.raw_payload:
            continue
        summaries.append(_summary_from_project(TemporalProject.model_validate(record.raw_payload)))
    return summaries

