from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import backends, cache, detection, files, health, jobs, releases, temporal_projects
from src.config import get_settings


def create_fastapi_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Building Change Detection API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(releases.router, prefix="/api/releases", tags=["releases"])
    app.include_router(backends.router, prefix="/api/backends", tags=["backends"])
    app.include_router(detection.router, prefix="/api/detection", tags=["detection"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(temporal_projects.router, prefix="/api/temporal-projects", tags=["temporal-projects"])
    app.include_router(cache.router, prefix="/api/cache", tags=["cache"])
    app.include_router(files.router, prefix="/api/files", tags=["files"])

    return app


app = create_fastapi_app()
