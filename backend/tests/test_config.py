from __future__ import annotations

from pathlib import Path

import pytest

import src.config as config_module
from src.config import Settings, get_settings


def test_settings_exposes_mapbox_max_tiles_per_request_default(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")

    assert settings.mapbox_max_tiles_per_request == 1024
    assert settings.mapbox_current_imagery_max_tiles == 1024
    assert settings.mapbox_current_imagery_default_zoom == 18
    assert settings.mapbox_current_imagery_max_zoom == 18


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


def test_settings_accepts_bandon_as_the_only_inference_backend(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="bandon_mps")

    assert settings.inference_backend == "bandon_mps"


def test_get_settings_reads_inference_timing_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_INFERENCE_TIMING_ENABLED", "true")

    settings = get_settings()

    assert settings.inference_timing_enabled is True


def test_get_settings_reads_bandon_inference_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_BANDON_INFERENCE_MODE", "persistent_runner")

    settings = get_settings()

    assert settings.bandon_inference_mode == "persistent_runner"


def test_get_settings_reads_bandon_persistent_runner_compat_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_BANDON_PERSISTENT_RUNNER_ENABLED", "true")

    settings = get_settings()

    assert settings.bandon_inference_mode == "persistent_runner"


def test_settings_rejects_invalid_bandon_inference_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APP_BANDON_INFERENCE_MODE"):
        Settings(runtime_cache_dir=tmp_path / "runtime", bandon_inference_mode="invalid")


def test_settings_rejects_bandon_backend_with_missing_selected_checkpoint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APP_BANDON_CHECKPOINT_PATH.*APP_INFERENCE_BACKEND=bandon_mps"):
        Settings(
            runtime_cache_dir=tmp_path / "runtime",
            inference_backend="bandon_mps",
            bandon_checkpoint_path=tmp_path / "missing-bandon.pth",
        )


def test_settings_rejects_invalid_inference_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported inference backend: unsupported. Supported backends: bandon_mps"):
        Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="unsupported")


def test_settings_rejects_removed_legacy_inference_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported inference backend: mtgcdnet_s2looking_mps. Supported backends: bandon_mps"):
        Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="mtgcdnet_s2looking_mps")


def test_get_settings_ignores_removed_s2looking_checkpoint_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_BANDON_CHECKPOINT_PATH", str(checkpoint))
    monkeypatch.setenv("APP_S2LOOKING_CHECKPOINT_PATH", str(tmp_path / "removed.pth"))

    settings = get_settings()

    assert settings.inference_backend == "bandon_mps"
    assert settings.bandon_checkpoint_path == checkpoint.resolve()
    assert not hasattr(settings, "s2looking_checkpoint_path")


def test_get_settings_validates_environment_selected_checkpoint_during_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    checkpoint = tmp_path / "mounted-bandon.pth"
    checkpoint.write_bytes(b"bandon")
    monkeypatch.setattr(config_module, "DEFAULT_BANDON_CHECKPOINT_PATH", tmp_path / "missing-image-default.pth")
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_BANDON_CHECKPOINT_PATH", str(checkpoint))

    settings = get_settings()

    assert settings.bandon_checkpoint_path == checkpoint.resolve()


def test_get_settings_rejects_missing_environment_selected_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    missing_checkpoint = tmp_path / "missing-mounted-bandon.pth"
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_BANDON_CHECKPOINT_PATH", str(missing_checkpoint))

    with pytest.raises(ValueError, match=str(missing_checkpoint)):
        get_settings()


def test_get_settings_uses_docker_runtime_cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", "/data/runtime_cache")
    monkeypatch.setenv("APP_PACKAGED_DEPLOYMENT", "true")
    monkeypatch.setenv("APP_BANDON_CHECKPOINT_PATH", str(checkpoint))
    monkeypatch.setattr(Settings, "ensure_runtime_cache_dirs", lambda self: None)

    settings = get_settings()

    assert settings.runtime_cache_dir == Path("/data/runtime_cache")
    assert settings.packaged_deployment is True


def test_packaged_settings_reject_noncanonical_runtime_cache(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APP_RUNTIME_CACHE_DIR=/data/runtime_cache"):
        Settings(runtime_cache_dir=tmp_path / "backend/runtime_cache", packaged_deployment=True)


def test_get_settings_reads_canonical_threshold_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_RUNTIME_CACHE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("APP_CHANGE_THRESHOLD", "0.37")
    monkeypatch.setenv("APP_SEMANTIC_THRESHOLD", "0.42")

    settings = get_settings()

    assert settings.change_threshold == 0.37
    assert settings.semantic_threshold == 0.42


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("change_threshold", 1.01, "APP_CHANGE_THRESHOLD"),
        ("semantic_threshold", -0.01, "APP_SEMANTIC_THRESHOLD"),
    ],
)
def test_settings_rejects_invalid_canonical_thresholds(
    tmp_path: Path,
    field: str,
    value: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(runtime_cache_dir=tmp_path / "runtime", **{field: value})
