from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import JobRecord, ProjectRecord
from src.db.session import session_scope
from src.repositories.payload_storage import externalize_payload_if_needed, payload_storage_path, resolve_payload_reference


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
    progress_details: dict[str, Any] | None = None,
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
                progress_details=progress_details,
                settings=settings,
                session=scoped_session,
            )

    record = _get_job(session, job_id)
    record.status = "running"
    record.progress = max(0, min(100, progress))
    record.stage = stage
    record.message = message or record.message or "The backend is advancing through the pipeline."
    if progress_details is not None:
        payload = dict(get_job_full_result(record.raw_result, settings=settings) or {})
        payload["progress_details"] = progress_details
        record.raw_result = payload
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

    resolved_settings = settings or Settings()
    record = _get_job(session, job_id)
    record.status = COMPLETED_JOB_STATUS
    record.progress = 100
    record.stage = "completed"
    record.message = "Artifacts are ready."
    record.result_run_id = result_run_id
    record.raw_result = (
        externalize_payload_if_needed(
            raw_result,
            settings=resolved_settings,
            table="jobs",
            column="raw_result",
            schema="job_result_v1",
            target_path=payload_storage_path(
                resolved_settings,
                table="jobs",
                column="raw_result",
                key=job_id,
                filename="raw_result.json",
            ),
        )
        if raw_result is not None
        else None
    )
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
    if raw_result is None:
        record.raw_result = None
    elif settings is None:
        # Failure persistence must not depend on fully validating runtime/model
        # settings. Keep the error payload inline when no settings were supplied.
        record.raw_result = raw_result
    else:
        record.raw_result = externalize_payload_if_needed(
            raw_result,
            settings=settings,
            table="jobs",
            column="raw_result",
            schema="job_result_v1",
            target_path=payload_storage_path(
                settings,
                table="jobs",
                column="raw_result",
                key=job_id,
                filename="raw_result.json",
            ),
        )
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


def list_job_summaries(
    *,
    limit: int = 50,
    status: str | None = None,
    job_kind: str | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    if session is None:
        with session_scope(settings) as scoped_session:
            return list_job_summaries(limit=limit, status=status, job_kind=job_kind, settings=settings, session=scoped_session)

    statement = select(
        JobRecord.job_id,
        JobRecord.celery_task_id,
        JobRecord.job_kind,
        JobRecord.status,
        JobRecord.project_id,
        JobRecord.request_hash,
        JobRecord.progress,
        JobRecord.stage,
        JobRecord.message,
        JobRecord.error_code,
        JobRecord.error_message,
        JobRecord.result_run_id,
        JobRecord.raw_request,
        JobRecord.cancel_requested,
        JobRecord.created_at,
        JobRecord.updated_at,
        JobRecord.started_at,
        JobRecord.completed_at,
    )
    if status:
        normalized_status = normalize_job_status(status)
        if normalized_status == COMPLETED_JOB_STATUS:
            statement = statement.where(JobRecord.status.in_((COMPLETED_JOB_STATUS, LEGACY_COMPLETED_JOB_STATUS)))
        else:
            statement = statement.where(JobRecord.status == normalized_status)
    if job_kind:
        statement = statement.where(JobRecord.job_kind == job_kind)
    rows = session.execute(statement.order_by(JobRecord.created_at.desc()).limit(limit)).all()
    return [
        {
            "job_id": row.job_id,
            "celery_task_id": row.celery_task_id,
            "job_kind": row.job_kind,
            "status": normalize_job_status(row.status),
            "project_id": row.project_id,
            "request_hash": row.request_hash,
            "progress": row.progress,
            "stage": row.stage,
            "message": row.message,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "result_run_id": row.result_run_id,
            "raw_request": row.raw_request,
            "cancel_requested": row.cancel_requested,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
        }
        for row in rows
    ]


def get_project_for_job(session: Session, project_id: str) -> ProjectRecord | None:
    return session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()


def get_job_full_result(raw_result: dict | None, *, settings: Settings | None = None) -> dict | None:
    return resolve_payload_reference(raw_result, settings=settings, table="jobs", column="raw_result")
