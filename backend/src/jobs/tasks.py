from __future__ import annotations

from time import perf_counter
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
from src.schemas import RunRequest, RunResponse, TemporalProjectRunResponse


class JobCancelledError(RuntimeError):
    pass


class StageTimer:
    def __init__(self) -> None:
        self._start = perf_counter()
        self._last = self._start
        self.stage_seconds: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        now = perf_counter()
        self.stage_seconds[stage] = round(now - self._last, 3)
        self._last = now

    def summary(self) -> dict[str, float]:
        return {
            "total": round(perf_counter() - self._start, 3),
            **self.stage_seconds,
        }


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


def _cancel_if_requested(job_id: str, settings: Settings) -> None:
    with session_scope(settings) as session:
        job = get_job(job_id, settings=settings, session=session)
        if job.cancel_requested or job.status == "cancel_requested":
            mark_job_cancelled(job_id=job_id, settings=settings, session=session)
            raise JobCancelledError("Job cancelled at a safe phase boundary.")


def _publish_progress(self: Any, job_id: str, progress: int, stage: str, message: str, settings: Settings) -> None:
    _cancel_if_requested(job_id, settings)
    update_progress(job_id, progress, stage, message, settings=settings)


def _artifact_summaries(items: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "path": item.path,
            "media_type": item.media_type,
            "description": item.description,
        }
        for item in items
    ]


def _compact_detection_result(response: RunResponse, *, result_run_id: str | None, stage_timings: dict[str, float]) -> dict[str, Any]:
    summary = response.summary.model_dump(mode="json") if response.summary is not None else {}
    diagnostics = response.diagnostics.model_dump(mode="json") if response.diagnostics is not None else {}
    return {
        "success": response.success,
        "error_code": response.error_code,
        "error_message": response.error_message,
        "result_run_id": result_run_id,
        "request_hash": summary.get("request_hash"),
        "project_id": None,
        "mode": summary.get("mode"),
        "model_backend": summary.get("model_backend"),
        "metrics": {
            "estimated_area_m2": summary.get("estimated_area_m2"),
            "total_new_buildings": summary.get("total_new_buildings"),
            "total_building_blocks": summary.get("total_building_blocks"),
            "total_new_building_area_m2": summary.get("total_new_building_area_m2"),
            "total_building_block_area_m2": summary.get("total_building_block_area_m2"),
            "total_change_polygons": summary.get("total_change_polygons"),
            "total_change_area_m2": summary.get("total_change_area_m2"),
        },
        "artifacts": _artifact_summaries(response.artifacts),
        "downloadable_zip_path": response.downloadable_zip_path,
        "pipeline_stage_seconds": diagnostics.get("stage_seconds", {}),
        "job_stage_seconds": stage_timings,
    }


def _compact_temporal_result(response: TemporalProjectRunResponse, *, result_run_id: str | None, stage_timings: dict[str, float]) -> dict[str, Any]:
    project = response.project
    complete_count = sum(1 for milestone in project.milestones if milestone.status == "complete")
    artifact_paths = [
        {"name": artifact.name, "path": artifact.path, "media_type": artifact.media_type}
        for milestone in project.milestones
        for artifact in milestone.artifacts
    ]
    return {
        "success": response.success,
        "error_code": None,
        "error_message": response.error_message,
        "project_id": project.project_id,
        "result_run_id": result_run_id,
        "milestone_count": len(project.milestones),
        "complete_milestone_count": complete_count,
        "download_bundle_path": project.download_bundle_path,
        "artifacts": artifact_paths,
        "job_stage_seconds": stage_timings,
    }


def _prepare_job_for_execution(job_id: str, settings: Settings) -> dict[str, Any] | None:
    with session_scope(settings) as session:
        job = get_job(job_id, settings=settings, session=session)
        if job.cancel_requested or job.status == "cancel_requested":
            mark_job_cancelled(job_id=job_id, settings=settings, session=session)
            return {"job_id": job_id, "status": "cancelled"}
        if job.status in TERMINAL_JOB_STATUSES:
            status = "completed" if job.status == "complete" else job.status
            return {"job_id": job_id, "status": status, "skipped": True}
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
    timer = StageTimer()
    try:
        skipped_result = _prepare_job_for_execution(job_id, settings)
        if skipped_result is not None:
            return skipped_result

        with session_scope(settings) as session:
            mark_job_running(job_id=job_id, stage="starting", progress=5, message="Backend worker started processing your request.", settings=settings, session=session)
        timer.mark("starting")

        _publish_progress(self, job_id, 12, "preflight", "Validating project state before the temporal run.", settings)
        timer.mark("preflight")
        _publish_progress(self, job_id, 25, "fetching_imagery", "Fetching or reusing milestone imagery.", settings)
        response = run_temporal_project_api(project_id, settings=settings, x_ip_token=None)
        timer.mark("processing")
        _publish_progress(self, job_id, 90, "saving_artifacts", "Saving temporal outputs and generated artifacts.", settings)
        result_run_id = _latest_temporal_run_id(response.project.project_id, settings) if response.success else None
        if response.success:
            timer.mark("saving_artifacts")
            _publish_progress(self, job_id, 95, "persisting", "Persisting compact job metadata.", settings)
            raw_result = _compact_temporal_result(response, result_run_id=result_run_id, stage_timings=timer.summary())
            with session_scope(settings) as session:
                mark_job_completed(
                    job_id=job_id,
                    result_run_id=result_run_id or response.project.project_id,
                    raw_result=raw_result,
                    settings=settings,
                    session=session,
                )
            return {"job_id": job_id, "status": "completed", "result_run_id": result_run_id, "project_id": response.project.project_id}

        with session_scope(settings) as session:
            mark_job_failed(
                job_id=job_id,
                error_code="runtime_error",
                error_message=response.error_message or "Temporal project run failed.",
                raw_result=_compact_temporal_result(response, result_run_id=result_run_id, stage_timings=timer.summary()),
                settings=settings,
                session=session,
            )
        return {"job_id": job_id, "status": "failed", "error_message": response.error_message}
    except JobCancelledError as exc:
        return {"job_id": job_id, "status": "cancelled"}
    except Exception as exc:  # noqa: BLE001
        mark_job_execution_failed(job_id, f"{type(exc).__name__}: {exc}", settings=settings)
        raise


@celery_app.task(bind=True, name="building_change.run_detection")
def run_detection_job(self, job_id: str, request_payload: dict[str, Any], settings_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = _resolve_settings(settings_payload)
    timer = StageTimer()
    try:
        request = RunRequest.model_validate(request_payload)
        skipped_result = _prepare_job_for_execution(job_id, settings)
        if skipped_result is not None:
            return skipped_result

        with session_scope(settings) as session:
            mark_job_running(job_id=job_id, stage="starting", progress=5, message="Backend worker started processing your request.", settings=settings, session=session)
        timer.mark("starting")

        _publish_progress(self, job_id, 12, "preflight", "Validating request and preparing execution backend.", settings)
        timer.mark("preflight")
        _publish_progress(self, job_id, 25, "fetching_imagery", "Fetching or reusing source imagery.", settings)

        def progress_callback(fraction: float, message: str) -> None:
            stage = "inference"
            if fraction < 0.35:
                stage = "fetching_imagery"
            elif fraction < 0.65:
                stage = "inference"
            elif fraction < 0.8:
                stage = "vectorizing"
            elif fraction < 0.92:
                stage = "building_buffers"
            else:
                stage = "saving_artifacts"
            progress_value = max(25, min(92, int(round(fraction * 100))))
            _publish_progress(self, job_id, progress_value, stage, message, settings)

        response = run_detection_api(
            request,
            settings=settings,
            execution_config=_build_execution_config(request, settings),
            progress_callback=progress_callback,
            x_ip_token=None,
        )
        timer.mark("processing")
        _publish_progress(self, job_id, 94, "persisting", "Persisting compact job metadata.", settings)
        result_run_id = None
        if response.success and response.summary:
            with session_scope(settings) as session:
                result_run_id = get_latest_detection_run_id(response.summary.request_hash, settings=settings, session=session)
        if response.success:
            raw_result = _compact_detection_result(response, result_run_id=result_run_id, stage_timings=timer.summary())
            with session_scope(settings) as session:
                mark_job_completed(
                    job_id=job_id,
                    result_run_id=result_run_id or (response.summary.request_hash if response.summary else None),
                    raw_result=raw_result,
                    settings=settings,
                    session=session,
                )
            return {"job_id": job_id, "status": "completed", "result_run_id": result_run_id}

        with session_scope(settings) as session:
            mark_job_failed(
                job_id=job_id,
                error_code=response.error_code,
                error_message=response.error_message or "Detection run failed.",
                raw_result=_compact_detection_result(response, result_run_id=result_run_id, stage_timings=timer.summary()),
                settings=settings,
                session=session,
            )
        return {"job_id": job_id, "status": "failed", "error_message": response.error_message}
    except JobCancelledError as exc:
        return {"job_id": job_id, "status": "cancelled"}
    except Exception as exc:  # noqa: BLE001
        mark_job_execution_failed(job_id, f"{type(exc).__name__}: {exc}", settings=settings)
        raise
