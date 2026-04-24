from __future__ import annotations

import gradio as gr
from pydantic import ValidationError as PydanticValidationError

from src.config import Settings
from src.core_api import (
    get_cached_run_response_api,
    get_temporal_project_api,
    import_temporal_override_api,
    list_releases_api,
    list_temporal_projects_api,
    probe_backends_api,
    run_detection_api,
    run_temporal_project_api,
    save_temporal_project_api,
    validate_request_api,
    validate_temporal_project_api,
)
from src.execution_profiles import PipelineExecutionConfig
from src.schemas import (
    ReleaseListResponse,
    RunRequest,
    RunResponse,
    TemporalOverrideRequest,
    TemporalProject,
    TemporalProjectRunResponse,
    TemporalProjectSaveResponse,
    TemporalProjectValidationResponse,
    ValidationRequest,
    ValidationResponse,
)
from src.utils.logging import get_logger


LOGGER = get_logger(__name__)


def _build_execution_config(request: ValidationRequest | RunRequest, settings: Settings) -> PipelineExecutionConfig:
    model_backend = request.model_backend or settings.model_backend_default
    if model_backend == "bandon_mps":
        return PipelineExecutionConfig(model_backend="bandon_mps")
    return PipelineExecutionConfig(
        model_backend="sam3",
        backend_mode=request.sam3_backend_mode or "public_zerogpu",
    )


def create_app(settings: Settings) -> gr.Blocks:
    with gr.Blocks(title="Building Change Detection Backend") as demo:
        gr.Markdown(
            """
            # Building Change Detection Backend

            This Gradio Space exposes named API endpoints for:

            - `/list_releases`
            - `/validate_request`
            - `/run_detection`
            - `/list_temporal_projects`
            - `/get_temporal_project`
            - `/save_temporal_project`
            - `/validate_temporal_project`
            - `/run_temporal_project`
            - `/import_temporal_override`

            The backend keeps Wayback acquisition and GIS post-processing local and supports both remote SAM3 and
            local BANDON MTGCDNet execution profiles.

            Use the separate static frontend for the full UI, or inspect `Use via API` in the footer for the
            generated contract.
            """
        )

        with gr.Column(visible=False):
            list_releases_trigger = gr.Button("list_releases_trigger")
            list_releases_response_json = gr.JSON(label="response")

            probe_backends_trigger = gr.Button("probe_backends_trigger")
            probe_backends_response_json = gr.JSON(label="response")

            validate_request_payload = gr.JSON(label="request")
            validate_request_trigger = gr.Button("validate_request_trigger")
            validate_request_response_json = gr.JSON(label="response")

            run_request_payload = gr.JSON(label="request")
            run_request_trigger = gr.Button("run_detection_trigger")
            run_request_response_json = gr.JSON(label="response")

            list_temporal_projects_payload = gr.JSON(label="request")
            list_temporal_projects_trigger = gr.Button("list_temporal_projects_trigger")
            list_temporal_projects_response_json = gr.JSON(label="response")

            get_temporal_project_payload = gr.JSON(label="request")
            get_temporal_project_trigger = gr.Button("get_temporal_project_trigger")
            get_temporal_project_response_json = gr.JSON(label="response")

            get_cached_run_response_payload = gr.JSON(label="request")
            get_cached_run_response_trigger = gr.Button("get_cached_run_response_trigger")
            get_cached_run_response_response_json = gr.JSON(label="response")

            save_temporal_project_payload = gr.JSON(label="request")
            save_temporal_project_trigger = gr.Button("save_temporal_project_trigger")
            save_temporal_project_response_json = gr.JSON(label="response")

            validate_temporal_project_payload = gr.JSON(label="request")
            validate_temporal_project_trigger = gr.Button("validate_temporal_project_trigger")
            validate_temporal_project_response_json = gr.JSON(label="response")

            run_temporal_project_payload = gr.JSON(label="request")
            run_temporal_project_trigger = gr.Button("run_temporal_project_trigger")
            run_temporal_project_response_json = gr.JSON(label="response")

            import_temporal_override_payload = gr.JSON(label="request")
            import_temporal_override_trigger = gr.Button("import_temporal_override_trigger")
            import_temporal_override_response_json = gr.JSON(label="response")

        def _list_releases() -> dict:
            return list_releases_api(settings=settings).model_dump(mode="json")

        def _probe_backends() -> list[dict]:
            return [
                item.model_dump(mode="json")
                for item in probe_backends_api(
                    settings=settings,
                )
            ]

        def _validate(request: dict) -> dict:
            try:
                parsed_request = ValidationRequest.model_validate(request)
            except PydanticValidationError as exc:
                return ValidationResponse(
                    valid=False,
                    normalized_aoi=None,
                    estimated_tile_count_t1=0,
                    estimated_tile_count_t2=0,
                    estimated_total_tiles=0,
                    estimated_area_m2=0.0,
                    warnings=[],
                    blocking_errors=[str(exc)],
                    recommended_mode="fast_preview",
                ).model_dump(mode="json")

            execution_config = _build_execution_config(parsed_request, settings)
            return validate_request_api(
                parsed_request,
                settings=settings,
                execution_config=execution_config,
            ).model_dump(mode="json")

        def _run(request: dict, request_context: gr.Request, progress=gr.Progress(track_tqdm=False)) -> dict:
            try:
                parsed_request = RunRequest.model_validate(request)
            except PydanticValidationError as exc:
                return RunResponse(
                    success=False,
                    error_code="invalid_request",
                    error_message=str(exc),
                ).model_dump(mode="json")

            try:
                execution_config = _build_execution_config(parsed_request, settings)
                return run_detection_api(
                    parsed_request,
                    settings=settings,
                    execution_config=execution_config,
                    progress_callback=lambda value, message: progress(value, desc=message),
                    x_ip_token=request_context.headers.get("x-ip-token"),
                ).model_dump(mode="json")
            except Exception as exc:
                LOGGER.exception("Run detection failed: %s", exc)
                return RunResponse(
                    success=False,
                    error_code="runtime_error",
                    error_message=f"{type(exc).__name__}: {exc}",
                ).model_dump(mode="json")

        def _list_temporal_projects(request: dict | None = None) -> list[dict]:
            include_cached_runs = bool(request.get("include_cached_runs")) if isinstance(request, dict) else False
            return [
                item.model_dump(mode="json")
                for item in list_temporal_projects_api(
                    settings=settings,
                    include_cached_runs=include_cached_runs,
                )
            ]

        def _get_temporal_project(request: dict) -> dict:
            project_id = request.get("project_id") if isinstance(request, dict) else None
            if not isinstance(project_id, str) or not project_id:
                return TemporalProjectRunResponse(
                    success=False,
                    error_message="project_id is required.",
                    project=TemporalProject(
                        project_id="invalid-request",
                        name="Invalid request",
                        created_at="1970-01-01T00:00:00Z",
                        updated_at="1970-01-01T00:00:00Z",
                ),
            ).project.model_dump(mode="json")
            return get_temporal_project_api(project_id, settings=settings).model_dump(mode="json")

        def _get_cached_run_response(request: dict) -> dict:
            request_hash = request.get("request_hash") if isinstance(request, dict) else None
            if not isinstance(request_hash, str) or not request_hash:
                return RunResponse(
                    success=False,
                    error_code="invalid_request",
                    error_message="request_hash is required.",
                ).model_dump(mode="json")
            return get_cached_run_response_api(request_hash, settings=settings).model_dump(mode="json")

        def _save_temporal_project(request: dict) -> dict:
            try:
                parsed_project = TemporalProject.model_validate(request.get("project") if isinstance(request, dict) else request)
            except PydanticValidationError as exc:
                raise ValueError(str(exc)) from exc
            saved_project = save_temporal_project_api(parsed_project, settings=settings)
            return TemporalProjectSaveResponse(
                project_id=saved_project.project_id,
                updated_at=saved_project.updated_at,
                download_bundle_path=saved_project.download_bundle_path,
            ).model_dump(mode="json")

        def _validate_temporal_project(request: dict) -> dict:
            try:
                parsed_project = TemporalProject.model_validate(request.get("project") if isinstance(request, dict) else request)
            except PydanticValidationError as exc:
                return TemporalProjectValidationResponse(
                    valid=False,
                    project=TemporalProject(
                        project_id="invalid-request",
                        name="Invalid request",
                        created_at="1970-01-01T00:00:00Z",
                        updated_at="1970-01-01T00:00:00Z",
                    ),
                    blocking_errors=[str(exc)],
                ).model_dump(mode="json")

            return validate_temporal_project_api(
                parsed_project,
                settings=settings,
            ).model_dump(mode="json")

        def _run_temporal_project(request: dict, request_context: gr.Request) -> dict:
            project_id = request.get("project_id") if isinstance(request, dict) else None
            if not isinstance(project_id, str) or not project_id:
                return TemporalProjectRunResponse(
                    success=False,
                    error_message="project_id is required.",
                    project=TemporalProject(
                        project_id="invalid-request",
                        name="Invalid request",
                        created_at="1970-01-01T00:00:00Z",
                        updated_at="1970-01-01T00:00:00Z",
                    ),
                ).model_dump(mode="json")

            return run_temporal_project_api(
                project_id,
                settings=settings,
                x_ip_token=request_context.headers.get("x-ip-token"),
            ).model_dump(mode="json")

        def _import_temporal_override(request: dict) -> dict:
            try:
                parsed_request = TemporalOverrideRequest.model_validate(request.get("request") if isinstance(request, dict) else request)
            except PydanticValidationError as exc:
                return TemporalProjectRunResponse(
                    success=False,
                    error_message=str(exc),
                    project=TemporalProject(
                        project_id="invalid-request",
                        name="Invalid request",
                        created_at="1970-01-01T00:00:00Z",
                        updated_at="1970-01-01T00:00:00Z",
                    ),
                ).model_dump(mode="json")
            return import_temporal_override_api(parsed_request, settings=settings).model_dump(mode="json")

        list_releases_trigger.click(
            fn=_list_releases,
            inputs=None,
            outputs=list_releases_response_json,
            api_name="list_releases",
            api_description="List ArcGIS Wayback releases discovered from the live WMTS capabilities document.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        probe_backends_trigger.click(
            fn=_probe_backends,
            inputs=None,
            outputs=probe_backends_response_json,
            api_name="probe_backends",
            api_description="Probe backend runtime availability for local BANDON and the configured SAM3 execution profiles.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        validate_request_trigger.click(
            fn=_validate,
            inputs=validate_request_payload,
            outputs=validate_request_response_json,
            api_name="validate_request",
            api_description="Normalize the AOI, validate release ordering, and estimate tile counts and area.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        run_request_trigger.click(
            fn=_run,
            inputs=run_request_payload,
            outputs=run_request_response_json,
            api_name="run_detection",
            api_description="Run the Wayback change-detection pipeline with the requested execution profile and export GIS-ready results.",
            concurrency_limit=1,
            concurrency_id="building-change-heavy",
            queue=True,
            show_progress="minimal",
        )

        list_temporal_projects_trigger.click(
            fn=_list_temporal_projects,
            inputs=list_temporal_projects_payload,
            outputs=list_temporal_projects_response_json,
            api_name="list_temporal_projects",
            api_description="List saved Temporal Mosaic projects.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        get_temporal_project_trigger.click(
            fn=_get_temporal_project,
            inputs=get_temporal_project_payload,
            outputs=get_temporal_project_response_json,
            api_name="get_temporal_project",
            api_description="Load a saved Temporal Mosaic project by project_id.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        get_cached_run_response_trigger.click(
            fn=_get_cached_run_response,
            inputs=get_cached_run_response_payload,
            outputs=get_cached_run_response_response_json,
            api_name="get_cached_run_response",
            api_description="Load a cached run response by request hash.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        save_temporal_project_trigger.click(
            fn=_save_temporal_project,
            inputs=save_temporal_project_payload,
            outputs=save_temporal_project_response_json,
            api_name="save_temporal_project",
            api_description="Persist a Temporal Mosaic project definition.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        validate_temporal_project_trigger.click(
            fn=_validate_temporal_project,
            inputs=validate_temporal_project_payload,
            outputs=validate_temporal_project_response_json,
            api_name="validate_temporal_project",
            api_description="Validate a Temporal Mosaic project, including ordered milestones and pairwise feasibility estimates.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        run_temporal_project_trigger.click(
            fn=_run_temporal_project,
            inputs=run_temporal_project_payload,
            outputs=run_temporal_project_response_json,
            api_name="run_temporal_project",
            api_description="Run the Temporal Mosaic workflow across the project's milestone sequence.",
            concurrency_limit=1,
            concurrency_id="building-change-heavy",
            queue=True,
            show_progress="minimal",
        )

        import_temporal_override_trigger.click(
            fn=_import_temporal_override,
            inputs=import_temporal_override_payload,
            outputs=import_temporal_override_response_json,
            api_name="import_temporal_override",
            api_description="Import a manual milestone override and recompute downstream cumulative outputs.",
            queue=False,
            concurrency_limit=None,
            show_progress="hidden",
        )

        demo.queue(default_concurrency_limit=1, max_size=16)

    return demo
