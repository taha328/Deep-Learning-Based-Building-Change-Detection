from __future__ import annotations

import redis
from fastapi import APIRouter, Depends, status
from sqlalchemy import text

from src.api.deps import get_app_settings
from src.api.errors import raise_api_error
from src.config import Settings
from src.db.session import session_scope


router = APIRouter()


def _broker_url(settings: Settings) -> str:
    return settings.celery_broker_url or settings.redis_url


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

    try:
        with session_scope(settings) as session:
            session.execute(text("SELECT 1")).scalar_one()
            postgis_version = session.execute(text("SELECT PostGIS_Version()")).scalar_one()
    except Exception:  # noqa: BLE001
        raise_api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_unavailable",
            "Could not connect to PostgreSQL/PostGIS. Run backend/scripts/setup_postgis_db.py --migrate and verify DATABASE_URL.",
            details={"persistence_backend": settings.persistence_backend},
        )

    return {
        "status": "ok",
        "database": "connected",
        "postgis": "available",
        "postgis_version": postgis_version,
        "persistence_backend": settings.persistence_backend,
    }


@router.get("/health/redis")
def redis_health(settings: Settings = Depends(get_app_settings)) -> dict[str, object]:
    if not settings.jobs_enabled:
        return {
            "status": "disabled",
            "redis": "not_configured",
            "jobs_enabled": False,
            "broker_url": _broker_url(settings),
        }

    try:
        client = redis.Redis.from_url(_broker_url(settings), socket_connect_timeout=2, socket_timeout=2)
        client.ping()
    except Exception as exc:  # noqa: BLE001
        raise_api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "redis_unavailable",
            "Could not connect to Redis. Start Redis before using async jobs.",
            details={"broker_url": _broker_url(settings), "error": str(exc)},
        )

    return {
        "status": "ok",
        "redis": "available",
        "jobs_enabled": True,
        "broker_url": _broker_url(settings),
        "celery_queue": settings.celery_task_default_queue,
    }


@router.get("/health/jobs")
def jobs_health(settings: Settings = Depends(get_app_settings)) -> dict[str, object]:
    if not settings.jobs_enabled:
        return {
            "status": "disabled",
            "jobs_enabled": False,
            "redis": "not_configured",
            "broker_url": _broker_url(settings),
            "celery_queue": settings.celery_task_default_queue,
            "celery_worker_pool": settings.celery_worker_pool,
        }

    redis_state = redis_health(settings)
    return {
        "status": "ok",
        "jobs_enabled": True,
        "redis": redis_state["redis"],
        "broker_url": _broker_url(settings),
        "celery_queue": settings.celery_task_default_queue,
        "celery_worker_pool": settings.celery_worker_pool,
    }
