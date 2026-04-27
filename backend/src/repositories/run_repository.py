from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import ProjectRecord, RunRecord
from src.db.session import session_scope
from src.repositories.artifact_repository import replace_run_artifacts
from src.schemas import RunRequest, RunResponse, TemporalProjectRunResponse


def _run_status(response: RunResponse | TemporalProjectRunResponse) -> str:
    return "complete" if response.success else "failed"


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

    request_hash = response.summary.request_hash if response.summary else None
    run_id = request_hash or f"detection-{datetime.now(UTC).timestamp()}"
    run = session.query(RunRecord).filter(RunRecord.run_id == run_id).one_or_none()
    if run is None:
        run = RunRecord(run_id=run_id, run_kind="detection")
        session.add(run)
        session.flush()

    run.request_hash = request_hash
    run.status = _run_status(response)
    run.mode = request.mode
    run.model_backend = response.summary.model_backend if response.summary else request.model_backend
    run.completed_at = datetime.now(UTC)
    run.error_code = response.error_code
    run.error_message = response.error_message
    run.raw_request = request.model_dump(mode="json")
    run.raw_response = response.model_dump(mode="json")
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

    project = session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()
    run_id = f"temporal-{project_id}"
    run = session.query(RunRecord).filter(RunRecord.run_id == run_id).one_or_none()
    if run is None:
        run = RunRecord(run_id=run_id, run_kind="temporal_project")
        session.add(run)
        session.flush()

    run.project_db_id = project.id if project else None
    run.status = _run_status(response)
    run.model_backend = (
        response.project.execution_config.model_backend
        if response.project.execution_config is not None
        else None
    )
    run.completed_at = datetime.now(UTC)
    run.error_message = response.error_message
    run.raw_request = {"project_id": project_id}
    run.raw_response = response.model_dump(mode="json")
    return response

