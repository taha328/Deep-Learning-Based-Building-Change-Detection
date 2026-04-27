from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import backends, cache, detection, files, health, releases, temporal_projects


def create_fastapi_app() -> FastAPI:
    app = FastAPI(title="Building Change Detection API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:5175",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
            "http://127.0.0.1:5175",
        ],
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(releases.router, prefix="/api/releases", tags=["releases"])
    app.include_router(backends.router, prefix="/api/backends", tags=["backends"])
    app.include_router(detection.router, prefix="/api/detection", tags=["detection"])
    app.include_router(temporal_projects.router, prefix="/api/temporal-projects", tags=["temporal-projects"])
    app.include_router(cache.router, prefix="/api/cache", tags=["cache"])
    app.include_router(files.router, prefix="/api/files", tags=["files"])

    return app


app = create_fastapi_app()
