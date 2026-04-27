from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError as PydanticValidationError

from src.api.deps import get_app_settings
from src.config import Settings
from src.core_api import run_detection_api, validate_request_api
from src.execution_profiles import PipelineExecutionConfig
from src.schemas import RunRequest, RunResponse, ValidationRequest, ValidationResponse


router = APIRouter()


def build_execution_config(request: ValidationRequest | RunRequest, settings: Settings) -> PipelineExecutionConfig:
    model_backend = request.model_backend or settings.model_backend_default
    if model_backend == "bandon_mps":
        return PipelineExecutionConfig(model_backend="bandon_mps")
    return PipelineExecutionConfig(
        model_backend="sam3",
        backend_mode=request.sam3_backend_mode or "public_zerogpu",
    )


@router.post("/validate")
def validate_detection(request: ValidationRequest, settings=Depends(get_app_settings)) -> ValidationResponse:
    execution_config = build_execution_config(request, settings)
    return validate_request_api(request, settings=settings, execution_config=execution_config)


@router.post("/run")
def run_detection(request: RunRequest, http_request: Request, settings=Depends(get_app_settings)) -> RunResponse:
    try:
        execution_config = build_execution_config(request, settings)
        return run_detection_api(
            request,
            settings=settings,
            execution_config=execution_config,
            progress_callback=None,
            x_ip_token=http_request.headers.get("x-ip-token"),
        )
    except PydanticValidationError as exc:
        return RunResponse(success=False, error_code="invalid_request", error_message=str(exc))
    except Exception as exc:  # noqa: BLE001
        return RunResponse(success=False, error_code="runtime_error", error_message=f"{type(exc).__name__}: {exc}")
