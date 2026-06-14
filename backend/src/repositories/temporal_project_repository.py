from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path
import time
from typing import Any

from sqlalchemy import case, func, insert, select, text
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.geometry import geojson_to_wkt_element, polygonal_geojson_to_geometry
from src.db.models import ArtifactRecord, GeometryLayerRecord, MilestoneMetricRecord, MilestoneRecord, ProjectRecord
from src.db.session import session_scope
from src.repositories.artifact_repository import artifact_mapping_from_entry
from src.repositories.payload_storage import (
    compute_sha256,
    externalize_payload_if_needed,
    payload_storage_path,
    resolve_payload_reference,
    write_json_payload_to_file,
)
from src.schemas import (
    TemporalMilestone,
    TemporalMilestoneMetrics,
    TemporalProject,
    TemporalProjectSummary,
    validate_stored_temporal_project,
)
from src.runtime_paths import temporal_project_dir
from src.utils.geometry import geodesic_area_m2


logger = logging.getLogger(__name__)

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


def _iso_datetime(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _summary_from_row(row: Any) -> TemporalProjectSummary:
    return TemporalProjectSummary(
        project_id=row.project_id,
        name=row.name,
        project_dir=row.project_dir,
        project_kind="temporal",
        display_name=row.name,
        semantics=row.semantics,
        milestone_count=int(row.milestone_count or 0),
        complete_milestone_count=int(row.complete_milestone_count or 0),
        created_at=_iso_datetime(row.created_at),
        updated_at=_iso_datetime(row.updated_at),
        download_bundle_path=row.download_bundle_path,
    )


def _project_summary_statement(project_id: str | None = None):
    bundle_path = (
        select(ArtifactRecord.path)
        .where(ArtifactRecord.project_db_id == ProjectRecord.id, ArtifactRecord.artifact_kind == "bundle")
        .order_by(ArtifactRecord.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        select(
            ProjectRecord.id.label("id"),
            ProjectRecord.project_id.label("project_id"),
            ProjectRecord.name.label("name"),
            ProjectRecord.semantics.label("semantics"),
            ProjectRecord.project_dir.label("project_dir"),
            ProjectRecord.created_at.label("created_at"),
            ProjectRecord.updated_at.label("updated_at"),
            func.count(MilestoneRecord.id).label("milestone_count"),
            func.coalesce(func.sum(case((MilestoneRecord.status == "complete", 1), else_=0)), 0).label("complete_milestone_count"),
            bundle_path.label("download_bundle_path"),
        )
        .select_from(ProjectRecord)
        .outerjoin(MilestoneRecord, MilestoneRecord.project_db_id == ProjectRecord.id)
        .group_by(
            ProjectRecord.id,
            ProjectRecord.project_id,
            ProjectRecord.name,
            ProjectRecord.semantics,
            ProjectRecord.project_dir,
            ProjectRecord.created_at,
            ProjectRecord.updated_at,
        )
    )
    if project_id is not None:
        statement = statement.where(ProjectRecord.project_id == project_id)
    return statement


def _project_summary_view_statement(project_id: str | None = None):
    where_clause = "WHERE project_id = :project_id" if project_id is not None else ""
    return text(
        f"""
        SELECT
            project_id,
            name,
            semantics,
            project_dir,
            created_at,
            updated_at,
            milestone_count,
            complete_milestone_count,
            download_bundle_path
        FROM public.project_summary
        {where_clause}
        ORDER BY updated_at DESC
        """
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


def _geometry_layer_mapping(
    *,
    project: ProjectRecord,
    milestone: MilestoneRecord | None,
    layer_kind: str,
    geojson: dict[str, Any] | None,
    settings: Settings,
    source: str | None = None,
) -> dict[str, Any] | None:
    if not geojson or layer_kind not in GEOMETRY_LAYER_KINDS:
        return None
    owner_key = f"{project.project_id}-{milestone.release_identifier if milestone else 'project'}-{layer_kind}"
    stored_geojson = externalize_payload_if_needed(
        geojson,
        settings=settings,
        table="geometry_layers",
        column="geojson",
        schema="geometry_layer_geojson_v1",
        target_path=payload_storage_path(
            settings,
            table="geometry_layers",
            column="geojson",
            key=owner_key,
            filename=f"{layer_kind}.json",
        ),
    )
    return {
        "project_db_id": project.id,
        "milestone_id": milestone.id if milestone else None,
        "layer_kind": layer_kind,
        "geom": geojson_to_wkt_element(geojson),
        "geojson": stored_geojson,
        "feature_count": _feature_count(geojson),
        "area_m2": _area_m2(geojson),
        "source": source,
    }


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

    resolved_settings = settings or Settings()
    project_dir = temporal_project_dir(resolved_settings, project.project_id, project.project_dir)
    project.project_dir = str(project_dir)
    payload = project.model_dump(mode="json")
    project_json_path = project_dir / "project.json"
    write_json_payload_to_file(payload, project_json_path)
    project_payload_reference = {
        "storage": "file",
        "path": str(project_json_path),
        "sha256": compute_sha256(project_json_path),
        "size_bytes": project_json_path.stat().st_size,
        "schema": "temporal_project_payload_v1",
    }
    logger.info(
        "DB_PAYLOAD_EXTERNALIZED table=%s column=%s sizeBytes=%s path=%s sha256=%s",
        "projects",
        "raw_payload",
        project_payload_reference["size_bytes"],
        project_json_path,
        project_payload_reference["sha256"],
    )
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
    record.raw_payload = project_payload_reference
    record.created_at = _parse_datetime(project.created_at) or record.created_at
    record.updated_at = _parse_datetime(project.updated_at) or datetime.now(UTC)

    session.query(GeometryLayerRecord).filter(GeometryLayerRecord.project_db_id == record.id).delete()
    session.query(ArtifactRecord).filter(ArtifactRecord.project_db_id == record.id).delete()
    session.query(MilestoneRecord).filter(MilestoneRecord.project_db_id == record.id).delete()
    session.flush()

    artifact_mappings: list[dict[str, object]] = []
    geometry_mappings: list[dict[str, Any]] = []
    aoi_mapping = _geometry_layer_mapping(
        project=record,
        milestone=None,
        layer_kind="aoi",
        geojson=project.aoi_geojson,
        settings=resolved_settings,
        source="project",
    )
    if aoi_mapping is not None:
        geometry_mappings.append(aoi_mapping)

    for milestone in project.milestones:
        milestone_record = MilestoneRecord(
            project_db_id=record.id,
            release_identifier=milestone.release_identifier,
            release_date=_parse_datetime(milestone.release_date),
            status=milestone.status,
            source_mode=milestone.source_mode,
            pair_request_hash=milestone.pair_request_hash,
            error_message=milestone.error_message,
            raw_payload={
                "storage": "summary",
                "schema": "temporal_milestone_summary_v1",
                "release_identifier": milestone.release_identifier,
                "release_date": milestone.release_date,
                "status": milestone.status,
                "source_mode": milestone.source_mode,
                "pair_request_hash": milestone.pair_request_hash,
                "error_message": milestone.error_message,
            },
        )
        session.add(milestone_record)
        session.flush()

        if milestone.metrics is not None:
            session.add(_metric_record(milestone_record, milestone.metrics))

        for entry in milestone.artifacts:
            artifact_mappings.append(artifact_mapping_from_entry(entry, project=record, milestone=milestone_record))

        for layer_kind, geojson in _milestone_geojson_layers(milestone):
            layer_mapping = _geometry_layer_mapping(
                project=record,
                milestone=milestone_record,
                layer_kind=layer_kind,
                geojson=geojson,
                settings=resolved_settings,
                source=milestone.source_mode,
            )
            if layer_mapping is not None:
                geometry_mappings.append(layer_mapping)

    if project.download_bundle_path:
        artifact_mappings.append(
            {
                "project_db_id": record.id,
                "milestone_id": None,
                "run_db_id": None,
                "name": f"{project.project_id}_temporal_project_bundle",
                "path": project.download_bundle_path,
                "media_type": "application/zip",
                "description": "Temporal project export bundle",
                "artifact_kind": "bundle",
                "size_bytes": None,
            }
        )
    if artifact_mappings:
        session.execute(insert(ArtifactRecord), artifact_mappings)
    if geometry_mappings:
        session.execute(insert(GeometryLayerRecord), geometry_mappings)
    return project


def get_project(project_id: str, *, settings: Settings | None = None, session: Session | None = None) -> TemporalProject:
    if session is None:
        with session_scope(settings) as scoped_session:
            return get_project(project_id, settings=settings, session=scoped_session)

    started_at = time.perf_counter()
    record = session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "DB_PROJECT_DETAIL_PAYLOAD_LOAD_MS projectId=%s rowCount=%s durationMs=%s",
        project_id,
        1 if record is not None else 0,
        duration_ms,
    )
    if record is None or not record.raw_payload:
        raise FileNotFoundError(f"Unknown temporal project: {project_id}")
    payload = resolve_payload_reference(record.raw_payload, settings=settings, table="projects", column="raw_payload")
    return validate_stored_temporal_project(payload)


def get_project_full_payload(project_id: str, *, settings: Settings | None = None, session: Session | None = None) -> TemporalProject:
    return get_project(project_id, settings=settings, session=session)


def list_project_summaries(
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[TemporalProjectSummary]:
    return list_project_summary_view(settings=settings, session=session)


def list_project_summary_view(
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[TemporalProjectSummary]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_project_summary_view(settings=settings, session=scoped_session)

    started_at = time.perf_counter()
    rows = session.execute(_project_summary_view_statement()).all()
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info("DB_PROJECT_LIST_QUERY_MS projectId=%s rowCount=%s durationMs=%s", None, len(rows), duration_ms)
    return [_summary_from_row(row) for row in rows]


def get_project_summary(
    project_id: str,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> TemporalProjectSummary:
    if session is None:
        with session_scope(settings) as scoped_session:
            return get_project_summary(project_id, settings=settings, session=scoped_session)

    started_at = time.perf_counter()
    row = session.execute(_project_summary_view_statement(project_id), {"project_id": project_id}).one_or_none()
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "DB_PROJECT_SUMMARY_QUERY_MS projectId=%s rowCount=%s durationMs=%s",
        project_id,
        1 if row is not None else 0,
        duration_ms,
    )
    if row is None:
        raise FileNotFoundError(f"Unknown temporal project: {project_id}")
    return _summary_from_row(row)


def list_project_milestone_summaries(
    project_id: str,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_project_milestone_summaries(project_id, settings=settings, session=scoped_session)

    started_at = time.perf_counter()
    rows = session.execute(
        select(
            MilestoneRecord.id,
            MilestoneRecord.release_identifier,
            MilestoneRecord.release_date,
            MilestoneRecord.status,
            MilestoneRecord.source_mode,
            MilestoneRecord.pair_request_hash,
            MilestoneRecord.error_message,
            MilestoneRecord.created_at,
            MilestoneRecord.updated_at,
            MilestoneMetricRecord.added_area_m2,
            MilestoneMetricRecord.total_area_m2,
            MilestoneMetricRecord.additions_feature_count,
            MilestoneMetricRecord.effective_feature_count,
            MilestoneMetricRecord.building_level_available,
            MilestoneMetricRecord.added_block_count,
            MilestoneMetricRecord.cumulative_block_count,
            MilestoneMetricRecord.added_block_area_m2,
            MilestoneMetricRecord.cumulative_block_area_m2,
            MilestoneMetricRecord.growth_envelope_area_m2,
        )
        .select_from(MilestoneRecord)
        .join(ProjectRecord, ProjectRecord.id == MilestoneRecord.project_db_id)
        .outerjoin(MilestoneMetricRecord, MilestoneMetricRecord.milestone_id == MilestoneRecord.id)
        .where(ProjectRecord.project_id == project_id)
        .order_by(MilestoneRecord.release_date.asc().nulls_last(), MilestoneRecord.release_identifier.asc())
    ).all()
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "DB_PROJECT_MILESTONE_SUMMARY_QUERY_MS projectId=%s rowCount=%s durationMs=%s",
        project_id,
        len(rows),
        duration_ms,
    )
    return [
        {
            "id": str(row.id),
            "release_identifier": row.release_identifier,
            "release_date": _iso_datetime(row.release_date) if row.release_date else None,
            "status": row.status,
            "source_mode": row.source_mode,
            "pair_request_hash": row.pair_request_hash,
            "error_message": row.error_message,
            "created_at": _iso_datetime(row.created_at),
            "updated_at": _iso_datetime(row.updated_at),
            "metrics": {
                "added_area_m2": row.added_area_m2,
                "total_area_m2": row.total_area_m2,
                "additions_feature_count": row.additions_feature_count,
                "effective_feature_count": row.effective_feature_count,
                "building_level_available": row.building_level_available,
                "added_block_count": row.added_block_count,
                "cumulative_block_count": row.cumulative_block_count,
                "added_block_area_m2": row.added_block_area_m2,
                "cumulative_block_area_m2": row.cumulative_block_area_m2,
                "growth_envelope_area_m2": row.growth_envelope_area_m2,
            }
            if row.added_area_m2 is not None
            else None,
        }
        for row in rows
    ]


def list_project_artifact_summaries(
    project_id: str,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_project_artifact_summaries(project_id, settings=settings, session=scoped_session)

    started_at = time.perf_counter()
    rows = session.execute(
        select(
            ArtifactRecord.id,
            ArtifactRecord.name,
            ArtifactRecord.path,
            ArtifactRecord.media_type,
            ArtifactRecord.description,
            ArtifactRecord.artifact_kind,
            ArtifactRecord.size_bytes,
            ArtifactRecord.checksum,
            ArtifactRecord.created_at,
            MilestoneRecord.release_identifier,
        )
        .select_from(ArtifactRecord)
        .join(ProjectRecord, ProjectRecord.id == ArtifactRecord.project_db_id)
        .outerjoin(MilestoneRecord, MilestoneRecord.id == ArtifactRecord.milestone_id)
        .where(ProjectRecord.project_id == project_id)
        .order_by(ArtifactRecord.created_at.desc())
    ).all()
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "DB_PROJECT_ARTIFACT_SUMMARY_QUERY_MS projectId=%s rowCount=%s durationMs=%s",
        project_id,
        len(rows),
        duration_ms,
    )
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "path": row.path,
            "media_type": row.media_type,
            "description": row.description,
            "artifact_kind": row.artifact_kind,
            "size_bytes": row.size_bytes,
            "checksum": row.checksum,
            "created_at": _iso_datetime(row.created_at),
            "release_identifier": row.release_identifier,
        }
        for row in rows
    ]


def list_projects(
    *,
    settings: Settings | None = None,
    session: Session | None = None,
    include_cached_runs: bool = False,
) -> list[TemporalProjectSummary]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_projects(settings=settings, session=scoped_session, include_cached_runs=include_cached_runs)

    return list_project_summaries(settings=settings, session=session)
