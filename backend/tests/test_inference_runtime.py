from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.core_api import probe_backends_api
from src.execution_profiles import PipelineExecutionConfig, resolve_backend, resolve_inference_runtime


def test_bandon_runtime_resolution_uses_bandon_checkpoint(tmp_path) -> None:
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", bandon_checkpoint_path=checkpoint)

    runtime = resolve_inference_runtime(settings)

    assert runtime.backend == "bandon_mps"
    assert runtime.checkpoint_env_var == "APP_BANDON_CHECKPOINT_PATH"
    assert runtime.checkpoint_path == checkpoint


def test_legacy_inference_backend_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unsupported inference backend: mtgcdnet_s2looking_mps. Supported backends: bandon_mps"):
        Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="mtgcdnet_s2looking_mps")


def test_execution_config_rejects_legacy_inference_backend() -> None:
    with pytest.raises(ValidationError, match="bandon_mps"):
        PipelineExecutionConfig(inference_backend="mtgcdnet_s2looking_mps")


def test_backend_availability_exposes_only_bandon(tmp_path, monkeypatch) -> None:
    class _Probe:
        available = True
        message = "ok"

        def diagnostics(self) -> dict[str, str]:
            return {}

    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", bandon_checkpoint_path=checkpoint)
    monkeypatch.setattr("src.execution_profiles.probe_bandon_runtime", lambda _settings: _Probe())

    availabilities = probe_backends_api(settings=settings)

    assert [item.mode for item in availabilities] == ["bandon_mps"]


def test_request_hash_context_changes_with_canonical_thresholds(tmp_path) -> None:
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    backend = resolve_backend(PipelineExecutionConfig(inference_backend="bandon_mps"))

    first = backend.request_hash_context(
        Settings(runtime_cache_dir=tmp_path / "runtime-a", bandon_checkpoint_path=checkpoint, change_threshold=0.37)
    )
    second = backend.request_hash_context(
        Settings(runtime_cache_dir=tmp_path / "runtime-b", bandon_checkpoint_path=checkpoint, change_threshold=0.44)
    )

    assert first["checkpoint_env_var_used"] == "APP_BANDON_CHECKPOINT_PATH"
    assert first["change_threshold"] == 0.37
    assert first != second


def test_request_hash_context_ignores_semantic_threshold_for_bandon_output_contract(tmp_path) -> None:
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    backend = resolve_backend(PipelineExecutionConfig(inference_backend="bandon_mps"))

    first = backend.request_hash_context(
        Settings(bandon_checkpoint_path=checkpoint, change_threshold=0.37, semantic_threshold=0.05)
    )
    second = backend.request_hash_context(
        Settings(bandon_checkpoint_path=checkpoint, change_threshold=0.37, semantic_threshold=0.95)
    )

    assert first == second
