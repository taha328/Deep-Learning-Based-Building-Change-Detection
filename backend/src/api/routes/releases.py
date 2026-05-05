from __future__ import annotations

from fastapi import APIRouter, Depends, status

from src.api.deps import get_app_settings
from src.api.errors import raise_api_error
from src.api.responses import model_json
from src.core_api import list_releases_api
from src.services.releases import ReleaseServiceError


router = APIRouter()


@router.get("")
def list_releases(settings=Depends(get_app_settings)) -> dict[str, object]:
    try:
        response = list_releases_api(settings=settings)
    except ReleaseServiceError as exc:
        raise_api_error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message, exc.details)
    return model_json(response)
