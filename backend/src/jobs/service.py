from __future__ import annotations

from typing import Any

import redis
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import JobRecord, ProjectRecord
from src.db.session import session_scope
from src.jobs.celery_app import celery_app
from src.jobs.exceptions import CeleryEnqueueError, JobNotFoundError, JobsDisabledError, RedisUnavailableError
from src.jobs.schemas import JobResponse, JobStartResponse
from src.repositories.job_repository import (
    create_job,
    get_job,
    list_jobs,
    mark_job_cancel_requested,
    mark_job_enqueued,
    mark_job_failed,
    normalize_job_status,
)
from src.schemas import RunRequest


def _broker_url(settings: Settings) -> str:
    return settings.celery_broker_url or settings.redis_url


def _redis_client(settings: Settings) -> redis.Redis:
    return redis.Redis.from_url(_broker_url(settings), socket_connect_timeout=2, socket_timeout=2)


def assert_jobs_enabled(settings: Settings) -> None:
    if not settings.jobs_enabled:
        raise JobsDisabledError(
            "Async jobs are disabled by configuration.",
            details={"jobs_enabled": settings.jobs_enabled},
        )


def assert_redis_available(settings: Settings) -> None:
    assert_jobs_enabled(settings)
    try:
        _redis_client(settings).ping()
    except Exception as exc:  # noqa: BLE001
        raise RedisUnavailableError(
            "Redis is unavailable. Start Redis before using async jobs.",
            details={"broker_url": _broker_url(settings), "error": str(exc)},
        )


def _job_response(job: JobRecord) -> JobResponse:
    return JobResponse.model_validate(
        {
            "job_id": job.job_id,
            "celery_task_id": job.celery_task_id,
            "job_kind": job.job_kind,
            "status": normalize_job_status(job.status),
            "project_id": job.project_id,
            "request_hash": job.request_hash,
            "progress": job.progress,
            "stage": job.stage,
            "message": job.message,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "result_run_id": job.result_run_id,
            "raw_request": job.raw_request,
            "raw_result": job.raw_result,
            "cancel_requested": job.cancel_requested,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
        }
    )


def start_temporal_project_job(project_id: str, *, settings: Settings) -> JobStartResponse:
    assert_redis_available(settings)
    enqueue_error: Exception | None = None
    job_id: str | None = None
    celery_task_id: str | None = None
    with session_scope(settings) as session:
        project = session.query(ProjectRecord).filter(ProjectRecord.project_id == project_id).one_or_none()
        if project is None:
            raise JobNotFoundError(
                f"Unknown temporal project: {project_id}",
                details={"project_id": project_id},
            )
        job_id = create_job(
            job_kind="temporal_project",
            session=session,
            project_db_id=project.id,
            project_id=project_id,
            raw_request={"project_id": project_id},
        ).job_id
    assert job_id is not None
    try:
        async_result = celery_app.send_task(
            "building_change.run_temporal_project",
            args=[job_id, project_id],
            kwargs={"settings_payload": None},
            queue=settings.celery_task_default_queue,
        )
    except Exception as exc:  # noqa: BLE001
        with session_scope(settings) as session:
            mark_job_failed(job_id=job_id, error_code="celery_unavailable", error_message=str(exc), session=session)
        enqueue_error = exc
    else:
        celery_task_id = async_result.id
        with session_scope(settings) as session:
            mark_job_enqueued(job_id=job_id, celery_task_id=async_result.id, session=session)

    if enqueue_error is not None:
        raise CeleryEnqueueError(
            "Could not enqueue async job. Ensure Redis and Celery worker are running.",
            details={"error": str(enqueue_error), "queue": settings.celery_task_default_queue},
        )

    assert job_id is not None and celery_task_id is not None
    return JobStartResponse(job_id=job_id, celery_task_id=celery_task_id, job_kind="temporal_project", status="queued")


def start_detection_job(request: RunRequest, *, settings: Settings) -> JobStartResponse:
    assert_redis_available(settings)
    enqueue_error: Exception | None = None
    job_id: str | None = None
    celery_task_id: str | None = None
    with session_scope(settings) as session:
        job_id = create_job(
            job_kind="detection",
            session=session,
            project_id=None,
            raw_request=request.model_dump(mode="json"),
        ).job_id
    assert job_id is not None
    try:
        async_result = celery_app.send_task(
            "building_change.run_detection",
            args=[job_id, request.model_dump(mode="json")],
            kwargs={"settings_payload": None},
            queue=settings.celery_task_default_queue,
        )
    except Exception as exc:  # noqa: BLE001
        with session_scope(settings) as session:
            mark_job_failed(job_id=job_id, error_code="celery_unavailable", error_message=str(exc), session=session)
        enqueue_error = exc
    else:
        celery_task_id = async_result.id
        with session_scope(settings) as session:
            mark_job_enqueued(job_id=job_id, celery_task_id=async_result.id, session=session)

    if enqueue_error is not None:
        raise CeleryEnqueueError(
            "Could not enqueue async job. Ensure Redis and Celery worker are running.",
            details={"error": str(enqueue_error), "queue": settings.celery_task_default_queue},
        )

    assert job_id is not None and celery_task_id is not None
    return JobStartResponse(job_id=job_id, celery_task_id=celery_task_id, job_kind="detection", status="queued")


def get_job_response(job_id: str, *, settings: Settings) -> JobResponse:
    try:
        return _job_response(get_job(job_id, settings=settings))
    except FileNotFoundError as exc:
        raise JobNotFoundError(str(exc), details={"job_id": job_id}) from exc


def list_job_responses(
    *,
    settings: Settings,
    limit: int = 50,
    status: str | None = None,
    job_kind: str | None = None,
) -> list[JobResponse]:
    return [_job_response(job) for job in list_jobs(settings=settings, limit=limit, status=status, job_kind=job_kind)]


def cancel_job(job_id: str, *, settings: Settings) -> JobResponse:
    try:
        with session_scope(settings) as session:
            job = mark_job_cancel_requested(job_id=job_id, session=session)
            if job.celery_task_id:
                # Revoke prevents queued tasks from starting; active tasks stop cooperatively at phase checks.
                celery_app.control.revoke(job.celery_task_id, terminate=False, signal="SIGTERM")
            return _job_response(job)
    except FileNotFoundError as exc:
        raise JobNotFoundError(str(exc), details={"job_id": job_id}) from exc


def mark_job_execution_failed(job_id: str, message: str, *, settings: Settings, error_code: str = "runtime_error") -> None:
    with session_scope(settings) as session:
        mark_job_failed(job_id=job_id, error_code=error_code, error_message=message, session=session)
