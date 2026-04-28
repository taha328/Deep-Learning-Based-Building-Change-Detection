from __future__ import annotations

from typing import Any

from src.config import Settings, get_settings
from src.core_api import run_detection_api, run_temporal_project_api
from src.db.session import session_scope
from src.execution_profiles import PipelineExecutionConfig
from src.jobs.celery_app import celery_app
from src.jobs.progress import update_progress
from src.jobs.service import mark_job_execution_failed
from src.repositories.job_repository import (
    TERMINAL_JOB_STATUSES,
    get_job,
    is_job_stale,
    mark_job_cancelled,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
)
from src.repositories.run_repository import get_latest_detection_run_id, get_latest_temporal_run_id
from src.schemas import RunRequest


def _resolve_settings(settings_payload: dict[str, Any] | None = None) -> Settings:
    if settings_payload:
        return Settings.model_validate(settings_payload)
    return get_settings()


def _build_execution_config(request: RunRequest, settings: Settings) -> PipelineExecutionConfig:
    model_backend = request.model_backend or settings.model_backend_default
    if model_backend == "bandon_mps":
        return PipelineExecutionConfig(model_backend="bandon_mps")
    return PipelineExecutionConfig(
        model_backend="sam3",
        backend_mode=request.sam3_backend_mode or "public_zerogpu",
    )


def _prepare_job_for_execution(job_id: str, settings: Settings) -> dict[str, Any] | None:
    with session_scope(settings) as session:
        job = get_job(job_id, settings=settings, session=session)
        if job.cancel_requested or job.status == "cancel_requested":
            mark_job_cancelled(job_id=job_id, settings=settings, session=session)
            return {"job_id": job_id, "status": "cancelled"}
        if job.status in TERMINAL_JOB_STATUSES:
            return {"job_id": job_id, "status": job.status, "skipped": True}
        if is_job_stale(job, stale_after_minutes=settings.celery_job_stale_after_minutes):
            message = f"Job exceeded the stale timeout of {settings.celery_job_stale_after_minutes} minute(s) before the worker could start it."
            mark_job_failed(
                job_id=job_id,
                error_code="worker_stale",
                error_message=message,
                settings=settings,
                session=session,
            )
            return {"job_id": job_id, "status": "failed", "error_code": "worker_stale", "skipped": True}
    return None


def _latest_temporal_run_id(project_id: str, settings: Settings) -> str | None:
    with session_scope(settings) as session:
        return get_latest_temporal_run_id(project_id, settings=settings, session=session)


@celery_app.task(bind=True, name="building_change.run_temporal_project")
def run_temporal_project_job(self, job_id: str, project_id: str, settings_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = _resolve_settings(settings_payload)
    try:
        skipped_result = _prepare_job_for_execution(job_id, settings)
        if skipped_result is not None:
            return skipped_result

        with session_scope(settings) as session:
            mark_job_running(job_id=job_id, stage="starting", progress=5, message="Backend worker started processing your request.", settings=settings, session=session)
        self.update_state(state="PROGRESS", meta={"progress": 5, "stage": "starting", "message": "Backend worker started processing your request."})

        update_progress(job_id, 12, "preflight", "The backend is advancing through the pipeline.", settings=settings)
        self.update_state(state="PROGRESS", meta={"progress": 12, "stage": "preflight", "message": "The backend is advancing through the pipeline."})
        response = run_temporal_project_api(project_id, settings=settings, x_ip_token=None)
        result_run_id = _latest_temporal_run_id(response.project.project_id, settings) if response.success else None
        if response.success:
            with session_scope(settings) as session:
                mark_job_completed(
                    job_id=job_id,
                    result_run_id=result_run_id or response.project.project_id,
                    raw_result=response.model_dump(mode="json"),
                    settings=settings,
                    session=session,
                )
            self.update_state(state="SUCCESS", meta={"progress": 100, "stage": "completed", "message": "Artifacts are ready."})
            return {"job_id": job_id, "status": "completed", "result_run_id": result_run_id, "project_id": response.project.project_id}

        with session_scope(settings) as session:
            mark_job_failed(
                job_id=job_id,
                error_code="runtime_error",
                error_message=response.error_message or "Temporal project run failed.",
                raw_result=response.model_dump(mode="json"),
                settings=settings,
                session=session,
            )
        self.update_state(state="FAILURE", meta={"progress": 100, "stage": "failed", "message": response.error_message or "Temporal project run failed."})
        return {"job_id": job_id, "status": "failed", "error_message": response.error_message}
    except Exception as exc:  # noqa: BLE001
        mark_job_execution_failed(job_id, f"{type(exc).__name__}: {exc}", settings=settings)
        self.update_state(state="FAILURE", meta={"progress": 100, "stage": "failed", "message": f"{type(exc).__name__}: {exc}"})
        raise


@celery_app.task(bind=True, name="building_change.run_detection")
def run_detection_job(self, job_id: str, request_payload: dict[str, Any], settings_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = _resolve_settings(settings_payload)
    try:
        request = RunRequest.model_validate(request_payload)
        skipped_result = _prepare_job_for_execution(job_id, settings)
        if skipped_result is not None:
            return skipped_result

        with session_scope(settings) as session:
            mark_job_running(job_id=job_id, stage="starting", progress=5, message="Backend worker started processing your request.", settings=settings, session=session)
        self.update_state(state="PROGRESS", meta={"progress": 5, "stage": "starting", "message": "Backend worker started processing your request."})

        update_progress(job_id, 15, "metadata", "The backend is advancing through the pipeline.", settings=settings)
        self.update_state(state="PROGRESS", meta={"progress": 15, "stage": "metadata", "message": "The backend is advancing through the pipeline."})

        response = run_detection_api(
            request,
            settings=settings,
            execution_config=_build_execution_config(request, settings),
            progress_callback=None,
            x_ip_token=None,
        )
        result_run_id = None
        if response.success and response.summary:
            with session_scope(settings) as session:
                result_run_id = get_latest_detection_run_id(response.summary.request_hash, settings=settings, session=session)
        if response.success:
            with session_scope(settings) as session:
                mark_job_completed(
                    job_id=job_id,
                    result_run_id=result_run_id or (response.summary.request_hash if response.summary else None),
                    raw_result=response.model_dump(mode="json"),
                    settings=settings,
                    session=session,
                )
            self.update_state(state="SUCCESS", meta={"progress": 100, "stage": "completed", "message": "Artifacts are ready."})
            return {"job_id": job_id, "status": "completed", "result_run_id": result_run_id}

        with session_scope(settings) as session:
            mark_job_failed(
                job_id=job_id,
                error_code=response.error_code,
                error_message=response.error_message or "Detection run failed.",
                raw_result=response.model_dump(mode="json"),
                settings=settings,
                session=session,
            )
        self.update_state(state="FAILURE", meta={"progress": 100, "stage": "failed", "message": response.error_message or "Detection run failed."})
        return {"job_id": job_id, "status": "failed", "error_message": response.error_message}
    except Exception as exc:  # noqa: BLE001
        mark_job_execution_failed(job_id, f"{type(exc).__name__}: {exc}", settings=settings)
        self.update_state(state="FAILURE", meta={"progress": 100, "stage": "failed", "message": f"{type(exc).__name__}: {exc}"})
        raise
