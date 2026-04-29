from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict

from src.api.deps import get_app_settings
from src.api.errors import raise_api_error
from src.core_api import (
    get_temporal_project_api,
    import_temporal_override_api,
    list_temporal_projects_api,
    run_temporal_project_api,
    save_temporal_project_api,
    validate_temporal_project_api,
)
from src.services.temporal_projects import create_temporal_project_bundle
from src.schemas import (
    TemporalOverrideRequest,
    TemporalProject,
    TemporalProjectRunResponse,
    TemporalProjectSaveRequest,
    TemporalProjectSaveResponse,
    TemporalProjectValidationResponse,
)


router = APIRouter()


class TemporalProjectOverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_geojson: dict[str, Any]


@router.get("")
def list_projects(
    include_cached_runs: bool = False,
    settings=Depends(get_app_settings),
) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in list_temporal_projects_api(settings=settings, include_cached_runs=include_cached_runs)]


@router.get("/{project_id}")
def get_project(project_id: str, settings=Depends(get_app_settings)) -> dict[str, object]:
    try:
        return get_temporal_project_api(project_id, settings=settings).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))


@router.post("")
def save_project(body: TemporalProjectSaveRequest, settings=Depends(get_app_settings)) -> TemporalProjectSaveResponse:
    saved_project = save_temporal_project_api(body.project, settings=settings)
    return TemporalProjectSaveResponse(
        project_id=saved_project.project_id,
        updated_at=saved_project.updated_at,
        download_bundle_path=saved_project.download_bundle_path,
    )


@router.post("/validate")
def validate_project(
    body: TemporalProjectSaveRequest,
    settings=Depends(get_app_settings),
) -> TemporalProjectValidationResponse:
    return validate_temporal_project_api(body.project, settings=settings)


@router.post("/{project_id}/run")
def run_project(
    project_id: str,
    request: Request,
    settings=Depends(get_app_settings),
) -> TemporalProjectRunResponse:
    try:
        return run_temporal_project_api(project_id, settings=settings, x_ip_token=request.headers.get("x-ip-token"))
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except Exception as exc:  # noqa: BLE001
        return TemporalProjectRunResponse(
            success=False,
            error_message=f"{type(exc).__name__}: {exc}",
            project=TemporalProject(
                project_id=project_id,
                name="Invalid request",
                created_at="1970-01-01T00:00:00Z",
                updated_at="1970-01-01T00:00:00Z",
            ),
        )


@router.post("/{project_id}/milestones/{release_identifier}/override")
def import_override(
    project_id: str,
    release_identifier: str,
    body: TemporalProjectOverrideBody,
    settings=Depends(get_app_settings),
) -> TemporalProjectRunResponse:
    request = TemporalOverrideRequest(
        project_id=project_id,
        release_identifier=release_identifier,
        override_geojson=body.override_geojson,
    )
    return import_temporal_override_api(request, settings=settings)


@router.post("/{project_id}/export-bundle")
def export_bundle(project_id: str, settings=Depends(get_app_settings)) -> dict[str, str]:
    try:
        bundle_path = create_temporal_project_bundle(project_id, settings=settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    return {"path": str(bundle_path)}
