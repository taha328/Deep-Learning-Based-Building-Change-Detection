from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from src.api.deps import get_app_settings
from src.config import Settings
from src.db.session import session_scope


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "building-change-api"}


@router.get("/health/db")
def database_health(settings: Settings = Depends(get_app_settings)) -> dict[str, object]:
    if settings.persistence_backend == "filesystem":
        return {
            "status": "disabled",
            "database": "not_configured",
            "postgis": "not_checked",
            "persistence_backend": settings.persistence_backend,
        }

    with session_scope(settings) as session:
        session.execute(text("SELECT 1")).scalar_one()
        postgis_version = session.execute(text("SELECT PostGIS_Version()")).scalar_one()

    return {
        "status": "ok",
        "database": "connected",
        "postgis": "available",
        "postgis_version": postgis_version,
        "persistence_backend": settings.persistence_backend,
    }
