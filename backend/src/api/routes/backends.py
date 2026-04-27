from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_app_settings
from src.api.responses import model_list_json
from src.core_api import probe_backends_api


router = APIRouter()


@router.get("")
def list_backends(settings=Depends(get_app_settings)) -> list[dict[str, object]]:
    return model_list_json(probe_backends_api(settings=settings))
