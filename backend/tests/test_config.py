from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings, get_settings


def test_settings_exposes_mapbox_max_tiles_per_request_default(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")

    assert settings.mapbox_max_tiles_per_request == 1024
    assert settings.mapbox_current_imagery_max_tiles == 1024


def test_settings_rejects_non_positive_mapbox_max_tiles_per_request(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mapbox_max_tiles_per_request"):
        Settings(runtime_cache_dir=tmp_path / "runtime", mapbox_max_tiles_per_request=0)


def test_get_settings_uses_new_mapbox_tile_limit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_MAPBOX_MAX_TILES_PER_REQUEST", "512")
    monkeypatch.setenv("MAPBOX_CURRENT_IMAGERY_MAX_TILES", "1024")

    settings = get_settings()

    assert settings.mapbox_max_tiles_per_request == 512
    assert settings.mapbox_current_imagery_max_tiles == 1024


def test_get_settings_runtime_mapbox_limit_and_wayback_zoom_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_MAPBOX_MAX_TILES_PER_REQUEST", "1024")
    monkeypatch.setenv("MAPBOX_CURRENT_IMAGERY_MAX_TILES", "1024")
    monkeypatch.setenv("APP_WAYBACK_DEFAULT_ZOOM", "18")
    monkeypatch.setenv("APP_TILE_ZOOM", "18")

    settings = get_settings()

    assert settings.mapbox_max_tiles_per_request == 1024
    assert settings.mapbox_current_imagery_max_tiles == 1024
    assert settings.zoom == 18
    assert settings.tile_zoom == 18
    assert settings.wayback_default_zoom == 18


def test_get_settings_falls_back_to_legacy_mapbox_tile_limit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_MAPBOX_MAX_TILES_PER_REQUEST", "")
    monkeypatch.setenv("MAPBOX_CURRENT_IMAGERY_MAX_TILES", "256")

    settings = get_settings()

    assert settings.mapbox_max_tiles_per_request == 256
    assert settings.mapbox_current_imagery_max_tiles == 256


def test_settings_accepts_bandon_inference_backend_without_s2looking_checkpoint(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="bandon_mps")

    assert settings.inference_backend == "bandon_mps"
    assert settings.s2looking_checkpoint_path is None


def test_settings_rejects_invalid_inference_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APP_INFERENCE_BACKEND"):
        Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="unsupported")


def test_settings_rejects_s2looking_backend_without_checkpoint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APP_S2LOOKING_CHECKPOINT_PATH is required"):
        Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="mtgcdnet_s2looking_mps")


def test_settings_accepts_s2looking_backend_with_checkpoint(tmp_path: Path) -> None:
    checkpoint = tmp_path / "mtgcdnet_s2looking_fp_finetuned_best.pth"
    checkpoint.write_bytes(b"checkpoint")

    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        inference_backend="mtgcdnet_s2looking_mps",
        s2looking_checkpoint_path=checkpoint,
        s2looking_change_threshold=0.5,
    )

    assert settings.inference_backend == "mtgcdnet_s2looking_mps"
    assert settings.s2looking_checkpoint_path == checkpoint.resolve()
    assert settings.s2looking_change_threshold == 0.5


def test_get_settings_reads_s2looking_backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    checkpoint = tmp_path / "mtgcdnet_s2looking_fp_finetuned_best.pth"
    checkpoint.write_bytes(b"checkpoint")
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_INFERENCE_BACKEND", "mtgcdnet_s2looking_mps")
    monkeypatch.setenv("APP_S2LOOKING_CHECKPOINT_PATH", str(checkpoint))
    monkeypatch.setenv("APP_S2LOOKING_CHANGE_THRESHOLD", "0.50")

    settings = get_settings()

    assert settings.inference_backend == "mtgcdnet_s2looking_mps"
    assert settings.s2looking_checkpoint_path == checkpoint.resolve()
    assert settings.s2looking_change_threshold == 0.5
