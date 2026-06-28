from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from src.api.routes import backends, cache, detection, dev, files, health, jobs, releases, temporal_projects
from src.config import get_settings
from src.domain.wayback_metrics import render_prometheus_text

logger = logging.getLogger(__name__)


def create_fastapi_app() -> FastAPI:
    settings = get_settings()
    logger.info("BACKEND_STARTUP_STAGE settings_loaded")
    app = FastAPI(title="Building Change Detection API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Content-Length", "Content-Type"],
    )

    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(releases.router, prefix="/api/releases", tags=["releases"])
    app.include_router(backends.router, prefix="/api/backends", tags=["backends"])
    app.include_router(detection.router, prefix="/api/detection", tags=["detection"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(temporal_projects.router, prefix="/api/temporal-projects", tags=["temporal-projects"])
    app.include_router(dev.router, prefix="/api/dev", tags=["dev"])
    app.include_router(cache.router, prefix="/api/cache", tags=["cache"])
    app.include_router(files.router, prefix="/api/files", tags=["files"])

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            render_prometheus_text(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    logger.info("BACKEND_STARTUP_STAGE routes_registered")
    logger.info("BACKEND_STARTUP_STAGE app_created")

    return app


app = create_fastapi_app()
