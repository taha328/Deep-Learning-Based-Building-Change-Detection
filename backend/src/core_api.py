from __future__ import annotations

import logging
from typing import Callable

from src.config import Settings, get_settings
from src.domain.wayback_tile_preflight_cache import cleanup_wayback_preflight_locks
from src.execution_profiles import (
    BackendAvailability,
    PipelineExecutionConfig,
    collect_backend_availability,
    resolve_backend,
    resolve_configured_inference_execution_config,
)
from src.domain.cache import load_cached_response
from src.schemas import (
    ReleaseListResponse,
    RunRequest,
    RunResponse,
    TemporalOverrideRequest,
    TemporalProject,
    TemporalProjectRunRequest,
    TemporalProjectRunResponse,
    TemporalProjectSummary,
    TemporalProjectValidationResponse,
    ValidationRequest,
    ValidationResponse,
    change_threshold_was_explicit,
)
from src.services.processing import run_detection
from src.services.releases import list_releases, list_releases_response
from src.services.temporal_projects import (
    get_temporal_project,
    import_temporal_override,
    list_temporal_projects,
    resolve_temporal_project_execution_config,
    run_temporal_project,
    save_temporal_project,
    validate_temporal_project,
)
from src.services.validation import validate_request


ProgressCallback = Callable[[float, str, dict[str, object] | None], None]
LOGGER = logging.getLogger(__name__)


def _request_hash_context_with_threshold(
    backend,
    settings: Settings,
    change_threshold: float,
    *,
    threshold_explicit: bool,
) -> dict[str, object]:
    context = backend.request_hash_context(settings)
    context.update(
        change_threshold=change_threshold,
        threshold_source="request_override" if threshold_explicit else "default",
    )
    return context


def _resolve_settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def _release_date_lookup(project: TemporalProject, settings: Settings) -> dict[str, str]:
    dates = {
        milestone.release_identifier: milestone.release_date
        for milestone in project.milestones
        if milestone.release_date
    }
    try:
        for release in list_releases(settings):
            dates.setdefault(release.identifier, str(release.release_date))
    except Exception:
        LOGGER.debug("Unable to enrich temporal progress release dates from Wayback releases.", exc_info=True)
    return dates


def _temporal_pair_positions(project: TemporalProject) -> dict[tuple[str, str], int]:
    return {
        (project.milestones[index - 1].release_identifier, project.milestones[index].release_identifier): index
        for index in range(1, len(project.milestones))
    }


def _temporal_pair_progress_details(
    *,
    details: dict[str, object] | None,
    pair_fraction: float,
    pair_stage: str,
    current_pair_index: int,
    total_pair_count: int,
    from_release_identifier: str,
    to_release_identifier: str,
    release_dates: dict[str, str],
) -> dict[str, object]:
    enriched: dict[str, object] = dict(details or {})
    enriched.update(
        {
            "temporal_progress_kind": "active_pair",
            "current_pair_index": current_pair_index,
            "total_pair_count": total_pair_count,
            "pair_fraction": max(0.0, min(1.0, pair_fraction)),
            "pair_stage": pair_stage,
            "from_release_identifier": from_release_identifier,
            "to_release_identifier": to_release_identifier,
            "from_release_date": release_dates.get(from_release_identifier),
            "to_release_date": release_dates.get(to_release_identifier),
        }
    )
    return enriched


def list_releases_api(*, settings: Settings | None = None) -> ReleaseListResponse:
    return list_releases_response(_resolve_settings(settings))


def validate_request_api(
    request: ValidationRequest,
    *,
    settings: Settings | None = None,
    execution_config: PipelineExecutionConfig | None = None,
) -> ValidationResponse:
    resolved_settings = _resolve_settings(settings)
    del execution_config
    resolved_execution_config = resolve_configured_inference_execution_config(resolved_settings)
    backend = resolve_backend(resolved_execution_config, settings=resolved_settings)
    validation, _ = validate_request(
        request,
        releases=list_releases(resolved_settings),
        settings=backend.configure_settings(resolved_settings),
        remote_patch_budget_enabled=backend.enforce_remote_patch_budget(),
        request_hash_context=_request_hash_context_with_threshold(
            backend,
            resolved_settings,
            request.change_threshold,
            threshold_explicit=change_threshold_was_explicit(request),
        ),
    )
    return validation


def probe_backends_api(
    *,
    settings: Settings | None = None,
    execution_config: PipelineExecutionConfig | None = None,
) -> list[BackendAvailability]:
    resolved_settings = _resolve_settings(settings)
    return collect_backend_availability(settings=resolved_settings, execution_config=execution_config)


def run_detection_api(
    request: RunRequest,
    *,
    settings: Settings | None = None,
    execution_config: PipelineExecutionConfig | None = None,
    progress_callback: ProgressCallback | None = None,
    x_ip_token: str | None = None,
) -> RunResponse:
    resolved_settings = _resolve_settings(settings)
    del execution_config
    resolved_execution_config = resolve_configured_inference_execution_config(resolved_settings)
    backend = resolve_backend(resolved_execution_config, settings=resolved_settings)
    availability = backend.availability(resolved_settings)
    if not availability.available:
        return RunResponse(
            success=False,
            error_code="backend_unavailable",
            error_message=availability.reason or f"{availability.label} is not available.",
        )

    configured_settings = backend.configure_settings(resolved_settings)
    response = run_detection(
        request,
        settings=configured_settings,
        progress=progress_callback,
        x_ip_token=x_ip_token,
        inference_runner=backend.create_inference_runner(configured_settings),
        model_backend=backend.model_backend,
        remote_patch_budget_enabled=backend.enforce_remote_patch_budget(),
        request_hash_context=_request_hash_context_with_threshold(
            backend,
            configured_settings,
            request.change_threshold,
            threshold_explicit=change_threshold_was_explicit(request),
        ),
    )
    if resolved_settings.persistence_backend == "postgres":
        from src.repositories.run_repository import save_detection_run

        save_detection_run(request=request, response=response, settings=resolved_settings)
    return response


def get_cached_run_response_api(request_hash: str, *, settings: Settings | None = None) -> RunResponse:
    resolved_settings = _resolve_settings(settings)
    cached_response = load_cached_response(resolved_settings, request_hash)
    if cached_response is not None:
        return cached_response
    return RunResponse(
        success=False,
        error_code="cache_miss",
        error_message=f"No cached run response was found for request hash {request_hash}.",
    )


def list_temporal_projects_api(
    *,
    settings: Settings | None = None,
    include_cached_runs: bool = False,
) -> list[TemporalProjectSummary]:
    return list_temporal_projects(_resolve_settings(settings), include_cached_runs=include_cached_runs)


def get_temporal_project_api(project_id: str, *, settings: Settings | None = None) -> TemporalProject:
    return get_temporal_project(project_id, _resolve_settings(settings))


def save_temporal_project_api(project: TemporalProject, *, settings: Settings | None = None) -> TemporalProject:
    return save_temporal_project(project, _resolve_settings(settings))


def validate_temporal_project_api(
    project: TemporalProject,
    *,
    settings: Settings | None = None,
    execution_config: PipelineExecutionConfig | None = None,
) -> TemporalProjectValidationResponse:
    resolved_settings = _resolve_settings(settings)
    del execution_config
    resolved_execution_config = resolve_configured_inference_execution_config(resolved_settings)
    backend = resolve_backend(resolved_execution_config, settings=resolved_settings)
    configured_settings = backend.configure_settings(resolved_settings)
    return validate_temporal_project(
        project,
        settings=configured_settings,
        remote_patch_budget_enabled=backend.enforce_remote_patch_budget(),
        request_hash_context=backend.request_hash_context(configured_settings),
        execution_config=resolved_execution_config,
    )


def run_temporal_project_api(
    project_id: str,
    *,
    settings: Settings | None = None,
    execution_config: PipelineExecutionConfig | None = None,
    progress_callback: ProgressCallback | None = None,
    x_ip_token: str | None = None,
    run_request: TemporalProjectRunRequest | None = None,
    job_id: str | None = None,
) -> TemporalProjectRunResponse:
    resolved_settings = _resolve_settings(settings)
    project = get_temporal_project(project_id, resolved_settings)
    cleanup_wayback_preflight_locks(
        "temporal_project_processing_start",
        settings=resolved_settings,
        source="core_api",
    )
    del execution_config
    resolved_execution_config = resolve_configured_inference_execution_config(resolved_settings)
    backend = resolve_backend(resolved_execution_config, settings=resolved_settings)
    availability = backend.availability(resolved_settings)
    if not availability.available:
        return TemporalProjectRunResponse(
            success=False,
            error_message=availability.reason or f"{availability.label} is not available.",
            project=project,
        )

    configured_settings = backend.configure_settings(resolved_settings)
    threshold_explicit = change_threshold_was_explicit(run_request)
    change_threshold = run_request.change_threshold if run_request is not None else configured_settings.change_threshold
    if threshold_explicit:
        configured_settings = configured_settings.model_copy(update={"change_threshold": change_threshold})
    request_hash_context = _request_hash_context_with_threshold(
        backend,
        configured_settings,
        change_threshold,
        threshold_explicit=threshold_explicit,
    )
    release_dates = _release_date_lookup(project, configured_settings)
    pair_positions = _temporal_pair_positions(project)
    total_pair_count = max(len(project.milestones) - 1, 0)
    fallback_pair_counter = 0
    LOGGER.info(
        "TEMPORAL_RUN_REQUEST_THRESHOLD projectId=%s jobId=%s changeThreshold=%s source=%s",
        project_id,
        job_id,
        configured_settings.change_threshold,
        "request_override" if threshold_explicit else "default",
    )

    def _pair_runner(request: RunRequest) -> RunResponse:
        nonlocal fallback_pair_counter
        fallback_pair_counter += 1
        current_pair_index = pair_positions.get(
            (request.t1_release, request.t2_release),
            min(fallback_pair_counter, total_pair_count) if total_pair_count else fallback_pair_counter,
        )

        def _progress(fraction: float, message: str, details: dict[str, object] | None = None) -> None:
            if progress_callback is None:
                return
            progress_callback(
                fraction,
                message,
                _temporal_pair_progress_details(
                    details=details,
                    pair_fraction=fraction,
                    pair_stage=message,
                    current_pair_index=current_pair_index,
                    total_pair_count=total_pair_count,
                    from_release_identifier=request.t1_release,
                    to_release_identifier=request.t2_release,
                    release_dates=release_dates,
                ),
            )

        return run_detection(
            request,
            settings=configured_settings,
            progress=_progress if progress_callback is not None else None,
            x_ip_token=x_ip_token,
            inference_runner=backend.create_inference_runner(configured_settings),
            model_backend=backend.model_backend,
            remote_patch_budget_enabled=backend.enforce_remote_patch_budget(),
            request_hash_context=request_hash_context,
        )

    response = run_temporal_project(
        project_id,
        settings=configured_settings,
        pair_runner=_pair_runner,
        remote_patch_budget_enabled=backend.enforce_remote_patch_budget(),
        request_hash_context=request_hash_context,
        execution_config=resolved_execution_config,
    )
    if resolved_settings.persistence_backend == "postgres":
        from src.repositories.run_repository import save_temporal_run
        from src.repositories.temporal_project_repository import save_project as save_project_record

        save_project_record(response.project, settings=resolved_settings)
        save_temporal_run(project_id=project_id, response=response, settings=resolved_settings)
    return response


def import_temporal_override_api(
    request: TemporalOverrideRequest,
    *,
    settings: Settings | None = None,
) -> TemporalProjectRunResponse:
    resolved_settings = _resolve_settings(settings)
    response = import_temporal_override(request, settings=resolved_settings)

    if resolved_settings.persistence_backend == "postgres":
        from src.repositories.temporal_project_repository import save_project as save_project_record

        save_project_record(response.project, settings=resolved_settings)

    return response
