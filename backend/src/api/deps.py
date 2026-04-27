from __future__ import annotations

from src.config import Settings, get_settings


def get_app_settings() -> Settings:
    return get_settings()
