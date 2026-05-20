from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import JobRecord, ProjectRecord
from src.db.session import session_scope


JobStatus = Literal["queued", "running", "completed", "failed", "cancel_requested", "cancelled"]
LEGACY_COMPLETED_JOB_STATUS = "complete"
COMPLETED_JOB_STATUS = "completed"
TERMINAL_JOB_STATUSES = (COMPLETED_JOB_STATUS, LEGACY_COMPLETED_JOB_STATUS, "failed", "cancelled")


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_job_status(status: str | None) -> str | None:
    if status == LEGACY_COMPLETED_JOB_STATUS:
        return COMPLETED_JOB_STATUS
    return status


def create_job(
    *,
    job_kind: str,
    settings: Settings | None = None,
    session: Session | None = None,
    job_id: str | None = None,
    project_db_id: UUID | None = None,
    project_id: str | None = None,
    request_hash: str | None = None,
    raw_request: dict[str, Any] | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return create_job(
                job_kind=job_kind,
                settings=settings,
                session=scoped_session,
                job_id=job_id,
                project_db_id=project_db_id,
                project_id=project_id,
                request_hash=request_hash,
                raw_request=raw_request,
            )

    record = JobRecord(
        job_id=job_id or f"job-{uuid4().hex}",
        job_kind=job_kind,
        status="queued",
        project_db_id=project_db_id,
        project_id=project_id,
        request_hash=request_hash,
        progress=0,
        stage="queued",
        message="Queued for execution.",
        raw_request=raw_request,
    )
    session.add(record)
    session.flush()
    return record


def _get_job(session: Session, job_id: str) -> JobRecord:
    record = session.query(JobRecord).filter(JobRecord.job_id == job_id).one_or_none()
    if record is None:
        raise FileNotFoundError(f"Unknown job: {job_id}")
    return record


def mark_job_enqueued(
    *,
    job_id: str,
    celery_task_id: str,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_job_enqueued(job_id=job_id, celery_task_id=celery_task_id, settings=settings, session=scoped_session)

    record = _get_job(session, job_id)
    record.celery_task_id = celery_task_id
    record.status = "queued"
    record.progress = 0
    record.stage = "queued"
    record.message = "Queued for execution."
    return record


def mark_job_running(
    *,
    job_id: str,
    stage: str = "starting",
    progress: int = 5,
    message: str | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_job_running(
                job_id=job_id,
                stage=stage,
                progress=progress,
                message=message,
                settings=settings,
                session=scoped_session,
            )

    record = _get_job(session, job_id)
    record.status = "running"
    record.stage = stage
    record.progress = progress
    record.message = message or "Backend worker started processing your request."
    if record.started_at is None:
        record.started_at = utc_now()
    return record


def update_job_progress(
    *,
    job_id: str,
    progress: int,
    stage: str,
    message: str | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return update_job_progress(
                job_id=job_id,
                progress=progress,
                stage=stage,
                message=message,
                settings=settings,
                session=scoped_session,
            )

    record = _get_job(session, job_id)
    record.status = "running"
    record.progress = max(0, min(100, progress))
    record.stage = stage
    record.message = message or record.message or "The backend is advancing through the pipeline."
    if record.started_at is None:
        record.started_at = utc_now()
    return record


def mark_job_completed(
    *,
    job_id: str,
    result_run_id: str | None = None,
    raw_result: dict[str, Any] | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_job_completed(
                job_id=job_id,
                result_run_id=result_run_id,
                raw_result=raw_result,
                settings=settings,
                session=scoped_session,
            )

    record = _get_job(session, job_id)
    record.status = COMPLETED_JOB_STATUS
    record.progress = 100
    record.stage = "completed"
    record.message = "Artifacts are ready."
    record.result_run_id = result_run_id
    record.raw_result = raw_result
    record.error_code = None
    record.error_message = None
    if record.completed_at is None:
        record.completed_at = utc_now()
    return record


def mark_job_failed(
    *,
    job_id: str,
    error_code: str | None = None,
    error_message: str | None = None,
    raw_result: dict[str, Any] | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_job_failed(
                job_id=job_id,
                error_code=error_code,
                error_message=error_message,
                raw_result=raw_result,
                settings=settings,
                session=scoped_session,
            )

    record = _get_job(session, job_id)
    record.status = "failed"
    record.progress = 100
    record.stage = "failed"
    record.error_code = error_code
    record.error_message = error_message
    record.raw_result = raw_result
    if record.completed_at is None:
        record.completed_at = utc_now()
    return record


def mark_job_cancel_requested(
    *,
    job_id: str,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_job_cancel_requested(job_id=job_id, settings=settings, session=scoped_session)

    record = _get_job(session, job_id)
    record.cancel_requested = True
    record.status = "cancel_requested"
    record.stage = "cancel_requested"
    record.message = "Cancellation requested."
    return record


def mark_job_cancelled(
    *,
    job_id: str,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_job_cancelled(job_id=job_id, settings=settings, session=scoped_session)

    record = _get_job(session, job_id)
    record.cancel_requested = True
    record.status = "cancelled"
    record.stage = "cancelled"
    record.message = "Job cancelled."
    record.completed_at = record.completed_at or utc_now()
    return record


def get_job(
    job_id: str,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
) -> JobRecord:
    if session is None:
        with session_scope(settings) as scoped_session:
            return get_job(job_id, settings=settings, session=scoped_session)
    return _get_job(session, job_id)


def list_jobs(
    *,
    limit: int = 50,
    status: str | None = None,
    job_kind: str | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[JobRecord]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_jobs(limit=limit, status=status, job_kind=job_kind, settings=settings, session=scoped_session)

    query = session.query(JobRecord)
    if status:
        normalized_status = normalize_job_status(status)
        if normalized_status == COMPLETED_JOB_STATUS:
            query = query.filter(JobRecord.status.in_((COMPLETED_JOB_STATUS, LEGACY_COMPLETED_JOB_STATUS)))
        else:
            query = query.filter(JobRecord.status == normalized_status)
    if job_kind:
        query = query.filter(JobRecord.job_kind == job_kind)
    return query.order_by(JobRecord.created_at.desc()).limit(limit).all()


def get_project_for_job(session: Session, project_id: str) -> ProjectRecord | None:
    return session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()
