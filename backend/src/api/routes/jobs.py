from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from src.api.deps import get_app_settings
from src.api.errors import raise_api_error
from src.config import Settings
from src.jobs.exceptions import CeleryEnqueueError, JobNotFoundError, JobsDisabledError, RedisUnavailableError
from src.jobs.schemas import JobResponse, JobStartResponse
from src.jobs.service import (
    cancel_job,
    get_job_response,
    list_job_responses,
    reconcile_stale_jobs,
    start_detection_job,
    start_temporal_project_job,
)
from src.schemas import RunRequest


router = APIRouter()


def _raise_job_service_error(exc: Exception) -> None:
    if isinstance(exc, JobNotFoundError):
        raise_api_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message, exc.details)
    if isinstance(exc, (JobsDisabledError, RedisUnavailableError, CeleryEnqueueError)):
        raise_api_error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message, exc.details)
    raise exc


@router.get("")
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
    job_kind: str | None = Query(default=None),
    settings: Settings = Depends(get_app_settings),
) -> list[JobResponse]:
    return list_job_responses(settings=settings, limit=limit, status=status_filter, job_kind=job_kind)


@router.get("/{job_id}")
def get_job(job_id: str, settings: Settings = Depends(get_app_settings)) -> JobResponse:
    try:
        return get_job_response(job_id, settings=settings)
    except (JobNotFoundError, JobsDisabledError, RedisUnavailableError, CeleryEnqueueError) as exc:
        _raise_job_service_error(exc)


@router.post("/{job_id}/cancel")
def cancel(job_id: str, settings: Settings = Depends(get_app_settings)) -> JobResponse:
    try:
        return cancel_job(job_id, settings=settings)
    except (JobNotFoundError, JobsDisabledError, RedisUnavailableError, CeleryEnqueueError) as exc:
        _raise_job_service_error(exc)


@router.post("/admin/reconcile-stale")
def reconcile_stale(settings: Settings = Depends(get_app_settings)) -> dict[str, int]:
    return {"failed_jobs": reconcile_stale_jobs(settings)}


@router.post("/detection")
def start_detection(request: RunRequest, settings: Settings = Depends(get_app_settings)) -> JobStartResponse:
    try:
        return start_detection_job(request, settings=settings)
    except (JobNotFoundError, JobsDisabledError, RedisUnavailableError, CeleryEnqueueError) as exc:
        _raise_job_service_error(exc)


@router.post("/temporal-projects/{project_id}")
def start_temporal_project(project_id: str, settings: Settings = Depends(get_app_settings)) -> JobStartResponse:
    try:
        return start_temporal_project_job(project_id, settings=settings)
    except (JobNotFoundError, JobsDisabledError, RedisUnavailableError, CeleryEnqueueError) as exc:
        _raise_job_service_error(exc)
