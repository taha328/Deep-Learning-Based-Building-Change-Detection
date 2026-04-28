from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from src.api.deps import get_app_settings
from src.api.errors import raise_api_error
from src.config import Settings
from src.jobs.schemas import JobResponse, JobStartResponse
from src.jobs.service import cancel_job, get_job_response, list_job_responses, start_detection_job, start_temporal_project_job
from src.schemas import RunRequest


router = APIRouter()


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
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))


@router.post("/{job_id}/cancel")
def cancel(job_id: str, settings: Settings = Depends(get_app_settings)) -> JobResponse:
    try:
        return cancel_job(job_id, settings=settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))


@router.post("/detection")
def start_detection(request: RunRequest, settings: Settings = Depends(get_app_settings)) -> JobStartResponse:
    return start_detection_job(request, settings=settings)


@router.post("/temporal-projects/{project_id}")
def start_temporal_project(project_id: str, settings: Settings = Depends(get_app_settings)) -> JobStartResponse:
    return start_temporal_project_job(project_id, settings=settings)
