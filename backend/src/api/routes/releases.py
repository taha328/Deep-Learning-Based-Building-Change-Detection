from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_app_settings
from src.api.responses import model_list_json
from src.core_api import list_releases_api


router = APIRouter()


@router.get("")
def list_releases(settings=Depends(get_app_settings)) -> dict[str, object]:
    response = list_releases_api(settings=settings)
    return {"releases": model_list_json(response.releases)}
