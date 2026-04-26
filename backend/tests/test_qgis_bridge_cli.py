from __future__ import annotations

from pathlib import Path

from src.qgis_bridge_cli import RunnerConfig, _settings_from_config


def test_settings_from_config_creates_runtime_cache_subdirectories(tmp_path: Path) -> None:
    runtime_cache_dir = tmp_path / "qgis-runtime-cache"

    settings = _settings_from_config(
        RunnerConfig(runtime_cache_dir=str(runtime_cache_dir)),
    )

    assert settings.runtime_cache_dir == runtime_cache_dir
    assert settings.request_cache_dir.exists()
    assert settings.temporal_projects_dir.exists()
    assert settings.wayback_mosaic_cache_dir.exists()
