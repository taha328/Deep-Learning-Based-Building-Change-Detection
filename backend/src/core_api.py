from __future__ import annotations

import logging
from typing import Callable

from src.config import Settings, get_settings
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


def _request_hash_context_with_threshold(backend, settings: Settings, change_threshold: float | None) -> dict[str, object]:
    context = backend.request_hash_context(settings)
    if change_threshold is not None:
        context.update(change_threshold=change_threshold, threshold_source="request_override")
    return context


def _resolve_settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


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
        request_hash_context=_request_hash_context_with_threshold(backend, resolved_settings, request.change_threshold),
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
        request_hash_context=_request_hash_context_with_threshold(backend, configured_settings, request.change_threshold),
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
) -> TemporalProjectRunResponse:
    resolved_settings = _resolve_settings(settings)
    project = get_temporal_project(project_id, resolved_settings)
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
    change_threshold = run_request.change_threshold if run_request is not None else None
    if change_threshold is not None:
        configured_settings = configured_settings.model_copy(update={"change_threshold": change_threshold})
    request_hash_context = _request_hash_context_with_threshold(backend, configured_settings, change_threshold)
    LOGGER.info(
        "TEMPORAL_RUN_REQUEST_THRESHOLD projectId=%s changeThreshold=%s source=%s",
        project_id,
        configured_settings.change_threshold,
        "request_override" if change_threshold is not None else "backend_settings_env",
    )

    def _pair_runner(request: RunRequest) -> RunResponse:
        return run_detection(
            request,
            settings=configured_settings,
            progress=progress_callback,
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
