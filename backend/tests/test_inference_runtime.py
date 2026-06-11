from __future__ import annotations

from src.config import Settings
from src.core_api import probe_backends_api
from src.execution_profiles import PipelineExecutionConfig, resolve_backend, resolve_inference_runtime


def test_bandon_runtime_resolution_uses_bandon_checkpoint(tmp_path) -> None:
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="bandon_mps", bandon_checkpoint_path=checkpoint)

    runtime = resolve_inference_runtime(settings)

    assert runtime.backend == "bandon_mps"
    assert runtime.checkpoint_path == checkpoint
    assert runtime.change_threshold == settings.change_threshold


def test_s2looking_runtime_resolution_uses_s2_checkpoint(tmp_path) -> None:
    checkpoint = tmp_path / "s2looking.pth"
    checkpoint.write_bytes(b"s2")
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        inference_backend="mtgcdnet_s2looking_mps",
        s2looking_checkpoint_path=checkpoint,
        change_threshold=0.52,
    )

    runtime = resolve_inference_runtime(settings)

    assert runtime.backend == "mtgcdnet_s2looking_mps"
    assert runtime.checkpoint_path == checkpoint
    assert runtime.change_threshold == 0.52


def test_s2looking_requires_checkpoint(tmp_path) -> None:
    try:
        Settings(runtime_cache_dir=tmp_path / "runtime", inference_backend="mtgcdnet_s2looking_mps")
    except ValueError as exc:
        assert "APP_S2LOOKING_CHECKPOINT_PATH is required" in str(exc)
    else:
        raise AssertionError("Expected missing S2Looking checkpoint to fail settings validation.")


def test_backend_availability_exposes_only_supported_backends(tmp_path, monkeypatch) -> None:
    class _Probe:
        available = True
        message = "ok"

        def diagnostics(self) -> dict[str, str]:
            return {}

    checkpoint = tmp_path / "s2looking.pth"
    checkpoint.write_bytes(b"s2")
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", s2looking_checkpoint_path=checkpoint)
    monkeypatch.setattr("src.execution_profiles.probe_bandon_runtime", lambda configured_settings: _Probe())

    availabilities = probe_backends_api(settings=settings)

    assert {item.mode for item in availabilities} == {"bandon_mps", "mtgcdnet_s2looking_mps"}


def test_request_hash_context_separates_backend_checkpoint_and_threshold(tmp_path) -> None:
    bandon_checkpoint = tmp_path / "bandon.pth"
    s2_checkpoint = tmp_path / "s2looking.pth"
    bandon_checkpoint.write_bytes(b"bandon-checkpoint")
    s2_checkpoint.write_bytes(b"s2looking-checkpoint")
    backend = resolve_backend(PipelineExecutionConfig(inference_backend="bandon_mps"), settings=Settings(runtime_cache_dir=tmp_path / "runtime-a"))

    bandon_settings = Settings(
        runtime_cache_dir=tmp_path / "runtime-b",
        inference_backend="bandon_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        change_threshold=0.35,
    )
    s2_settings = Settings(
        runtime_cache_dir=tmp_path / "runtime-c",
        inference_backend="mtgcdnet_s2looking_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        s2looking_checkpoint_path=s2_checkpoint,
        change_threshold=0.50,
        semantic_threshold=0.61,
    )

    bandon_context = backend.request_hash_context(bandon_settings)
    s2_context = resolve_backend(PipelineExecutionConfig(inference_backend="mtgcdnet_s2looking_mps"), settings=s2_settings).request_hash_context(s2_settings)

    assert bandon_context["inference_backend"] == "bandon_mps"
    assert s2_context["inference_backend"] == "mtgcdnet_s2looking_mps"
    assert bandon_context["checkpoint_sha256"] != s2_context["checkpoint_sha256"]
    assert bandon_context["change_threshold"] != s2_context["change_threshold"]
    assert bandon_context["threshold_source"] == "backend_settings_env"
    assert s2_context["semantic_threshold"] == 0.61


def test_request_hash_context_changes_with_canonical_thresholds(tmp_path) -> None:
    checkpoint = tmp_path / "bandon.pth"
    checkpoint.write_bytes(b"bandon")
    backend = resolve_backend(PipelineExecutionConfig(inference_backend="bandon_mps"))

    first = backend.request_hash_context(
        Settings(
            runtime_cache_dir=tmp_path / "runtime-a",
            bandon_checkpoint_path=checkpoint,
            change_threshold=0.37,
            semantic_threshold=0.42,
        )
    )
    second = backend.request_hash_context(
        Settings(
            runtime_cache_dir=tmp_path / "runtime-b",
            bandon_checkpoint_path=checkpoint,
            change_threshold=0.44,
            semantic_threshold=0.61,
        )
    )

    assert first["change_threshold"] == 0.37
    assert first["semantic_threshold"] == 0.42
    assert first != second
