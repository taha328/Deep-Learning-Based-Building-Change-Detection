from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import JobRecord, ProjectRecord
from src.db.session import session_scope


JobStatus = Literal["queued", "running", "complete", "failed", "cancel_requested", "cancelled"]
STALE_JOB_STATUSES = ("queued", "running")
TERMINAL_JOB_STATUSES = ("complete", "failed", "cancelled")


def utc_now() -> datetime:
    return datetime.now(UTC)


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
    record.status = "complete"
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


def is_job_stale(record: JobRecord, *, stale_after_minutes: int) -> bool:
    if record.status not in STALE_JOB_STATUSES:
        return False
    stale_before = utc_now() - timedelta(minutes=max(1, stale_after_minutes))
    reference_time = record.started_at or record.updated_at or record.created_at
    if reference_time is None:
        return False
    return reference_time < stale_before


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
        query = query.filter(JobRecord.status == status)
    if job_kind:
        query = query.filter(JobRecord.job_kind == job_kind)
    return query.order_by(JobRecord.created_at.desc()).limit(limit).all()


def mark_stale_jobs_failed(
    *,
    stale_after_minutes: int,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[JobRecord]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return mark_stale_jobs_failed(stale_after_minutes=stale_after_minutes, settings=settings, session=scoped_session)

    stale_before = utc_now() - timedelta(minutes=max(1, stale_after_minutes))
    query = (
        session.query(JobRecord)
        .filter(JobRecord.status.in_(STALE_JOB_STATUSES))
        .filter(func.coalesce(JobRecord.started_at, JobRecord.updated_at, JobRecord.created_at) < stale_before)
    )
    stale_jobs = query.all()
    if not stale_jobs:
        return []

    message = f"Job exceeded the stale timeout of {stale_after_minutes} minute(s) without completing."
    for record in stale_jobs:
        record.status = "failed"
        record.stage = "failed"
        record.progress = 100
        record.error_code = "worker_stale"
        record.error_message = message
        record.message = message
        record.completed_at = record.completed_at or utc_now()
    return stale_jobs


def get_project_for_job(session: Session, project_id: str) -> ProjectRecord | None:
    return session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()
