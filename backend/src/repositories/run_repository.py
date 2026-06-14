from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import ProjectRecord, RunRecord
from src.db.session import session_scope
from src.repositories.artifact_repository import replace_run_artifacts
from src.repositories.payload_storage import externalize_payload_if_needed, payload_storage_path, resolve_payload_reference
from src.schemas import RunRequest, RunResponse, TemporalProjectRunResponse


def _run_status(response: RunResponse | TemporalProjectRunResponse) -> str:
    return "complete" if response.success else "failed"


def build_detection_run_id(request_hash: str | None) -> str:
    return request_hash or f"detection-{uuid.uuid4().hex}"


def build_temporal_run_id(project_id: str) -> str:
    return f"temporal-{project_id}-{uuid.uuid4().hex}"


def get_latest_detection_run_id(
    request_hash: str,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> str | None:
    if session is None:
        with session_scope(settings) as scoped_session:
            return get_latest_detection_run_id(request_hash, settings=settings, session=scoped_session)

    record = (
        session.query(RunRecord)
        .filter(RunRecord.request_hash == request_hash, RunRecord.run_kind == "detection")
        .order_by(RunRecord.created_at.desc())
        .first()
    )
    return record.run_id if record else None


def get_latest_temporal_run_id(
    project_id: str,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> str | None:
    if session is None:
        with session_scope(settings) as scoped_session:
            return get_latest_temporal_run_id(project_id, settings=settings, session=scoped_session)

    record = (
        session.query(RunRecord)
        .join(ProjectRecord, ProjectRecord.id == RunRecord.project_db_id)
        .filter(RunRecord.run_kind == "temporal_project", ProjectRecord.project_id == project_id)
        .order_by(RunRecord.created_at.desc())
        .first()
    )
    return record.run_id if record else None


def save_detection_run(
    *,
    request: RunRequest,
    response: RunResponse,
    settings: Settings | None = None,
    session: Session | None = None,
) -> RunResponse:
    if session is None:
        with session_scope(settings) as scoped_session:
            return save_detection_run(request=request, response=response, settings=settings, session=scoped_session)

    resolved_settings = settings or Settings()
    status = _run_status(response)
    request_hash = response.summary.request_hash if response.summary else None
    run_id = build_detection_run_id(request_hash)
    run = session.query(RunRecord).filter(RunRecord.run_id == run_id).one_or_none()
    if run is None:
        run = RunRecord(run_id=run_id, run_kind="detection", status=status)
        session.add(run)
        session.flush()

    run.request_hash = request_hash
    run.status = status
    run.mode = request.mode
    run.model_backend = response.summary.model_backend if response.summary else request.inference_backend
    run.completed_at = datetime.now(UTC)
    run.error_code = response.error_code
    run.error_message = response.error_message
    run.raw_request = request.model_dump(mode="json")
    run.raw_response = externalize_payload_if_needed(
        response.model_dump(mode="json"),
        settings=resolved_settings,
        table="runs",
        column="raw_response",
        schema="detection_run_response_v1",
        target_path=payload_storage_path(
            resolved_settings,
            table="runs",
            column="raw_response",
            key=run.run_id,
            filename="raw_response.json",
        ),
    )
    replace_run_artifacts(session, run, response.artifacts)
    return response


def save_temporal_run(
    *,
    project_id: str,
    response: TemporalProjectRunResponse,
    settings: Settings | None = None,
    session: Session | None = None,
) -> TemporalProjectRunResponse:
    if session is None:
        with session_scope(settings) as scoped_session:
            return save_temporal_run(project_id=project_id, response=response, settings=settings, session=scoped_session)

    resolved_settings = settings or Settings()
    response_project_id = response.project.project_id if response.project else project_id
    project = session.query(ProjectRecord).filter(ProjectRecord.project_id == response_project_id).one_or_none()
    status = _run_status(response)
    run = RunRecord(
        run_id=build_temporal_run_id(response_project_id),
        run_kind="temporal_project",
        status=status,
        project_db_id=project.id if project else None,
    )
    session.add(run)
    session.flush()

    run.project_db_id = project.id if project else None
    run.status = status
    run.model_backend = (
        response.project.execution_config.inference_backend
        if response.project.execution_config is not None
        else None
    )
    run.completed_at = datetime.now(UTC)
    run.error_message = response.error_message
    run.raw_request = {"project_id": response_project_id}
    run.raw_response = externalize_payload_if_needed(
        response.model_dump(mode="json"),
        settings=resolved_settings,
        table="runs",
        column="raw_response",
        schema="temporal_run_response_v1",
        target_path=payload_storage_path(
            resolved_settings,
            table="runs",
            column="raw_response",
            key=run.run_id,
            filename="raw_response.json",
        ),
    )
    return response


def get_run_full_response(raw_response: dict | None) -> dict | None:
    return resolve_payload_reference(raw_response, settings=settings, table="runs", column="raw_response")
