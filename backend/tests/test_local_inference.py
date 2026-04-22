from __future__ import annotations

import pytest

from src.domain.local_inference import LocalInferenceConfig, LocalRuntimeProbe, probe_local_runtime


def test_probe_local_runtime_prefers_transformers_on_darwin_mps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.domain.local_inference.platform.system", lambda: "Darwin")
    monkeypatch.setattr("src.domain.local_inference.detect_local_device", lambda preference: "mps")
    monkeypatch.setattr(
        "src.domain.local_inference._probe_repo_runtime",
        lambda config: LocalRuntimeProbe(available=False, message="repo import failed"),
    )
    monkeypatch.setattr(
        "src.domain.local_inference._probe_transformers_runtime",
        lambda config: LocalRuntimeProbe(
            available=True,
            message="Transformers SAM3 runtime detected on mps. Model source: facebook/sam3",
            device="mps",
            implementation="transformers",
            model_source="facebook/sam3",
        ),
    )

    probe = probe_local_runtime(LocalInferenceConfig(device_preference="auto"))

    assert probe.available is True
    assert probe.implementation == "transformers"
    assert probe.device == "mps"
    assert "preferred runtime path" in probe.message
    assert "repo import failed" in probe.message


def test_probe_local_runtime_uses_repo_first_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.domain.local_inference.platform.system", lambda: "Linux")
    monkeypatch.setattr("src.domain.local_inference.detect_local_device", lambda preference: "cpu")
    monkeypatch.setattr(
        "src.domain.local_inference._probe_repo_runtime",
        lambda config: LocalRuntimeProbe(
            available=True,
            message="Repo runtime available",
            device="cpu",
            implementation="official_repo",
            model_source="facebook/sam3",
        ),
    )
    monkeypatch.setattr(
        "src.domain.local_inference._probe_transformers_runtime",
        lambda config: LocalRuntimeProbe(available=True, message="Transformers available"),
    )

    probe = probe_local_runtime(LocalInferenceConfig(device_preference="cpu"))

    assert probe.available is True
    assert probe.implementation == "official_repo"
    assert probe.message == "Repo runtime available"
