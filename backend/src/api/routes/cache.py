from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_app_settings
from src.core_api import get_cached_run_response_api
from src.schemas import RunResponse


router = APIRouter()


@router.get("/runs/{request_hash}")
def get_cached_run_response(request_hash: str, settings=Depends(get_app_settings)) -> RunResponse:
    return get_cached_run_response_api(request_hash, settings=settings)
