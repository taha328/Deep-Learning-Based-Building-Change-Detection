from __future__ import annotations

import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image

from src.config import Settings
from src.domain.bandon_runner import _resolve_launcher, probe_bandon_runtime, run_bandon_inference


def _settings(tmp_path: Path) -> Settings:
    repo_dir = tmp_path / "BANDON-mps"
    env_prefix = repo_dir / ".conda-macos-mps"
    config_path = repo_dir / "workdirs_bandon" / "MTGCDNet" / "config.py"
    checkpoint_path = repo_dir / "checkpoints" / "mtgcdnet_iter_40000.pth"
    runner_path = repo_dir / "tools" / "infer_mps.py"
    (repo_dir / "workdirs_bandon" / "MTGCDNet").mkdir(parents=True)
    (repo_dir / "checkpoints").mkdir(parents=True)
    (repo_dir / "tools").mkdir(parents=True)
    (env_prefix / "bin").mkdir(parents=True)
    config_path.write_text("config = {}", encoding="utf-8")
    checkpoint_path.write_text("checkpoint", encoding="utf-8")
    runner_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (env_prefix / "bin" / "python").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return Settings(
        bandon_repo_dir=repo_dir,
        bandon_env_prefix=env_prefix,
        bandon_config_path=config_path,
        bandon_checkpoint_path=checkpoint_path,
        bandon_device="mps",
        bandon_allow_mps_fallback=False,
    )


def test_probe_bandon_runtime_reports_success(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    def fake_run(*args, **kwargs):
        del args, kwargs
        return subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps(
                {
                    "python_executable": str(settings.bandon_env_prefix / "bin" / "python"),
                    "torch_version": "2.8.0",
                    "mmcv_version": "1.7.0",
                    "mps_built": True,
                    "mps_available": True,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("src.domain.bandon_runner.shutil.which", lambda command: None)
    monkeypatch.setattr("src.domain.bandon_runner._run_command", fake_run)
    probe = probe_bandon_runtime(settings)
    assert probe.available is True
    assert probe.mps_available is True
    assert probe.torch_version == "2.8.0"


def test_run_bandon_inference_builds_command_and_parses_outputs(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(image_a)
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(image_b)
    np.save(out_dir / "change_probability.npy", np.full((4, 4), 0.8, dtype=np.float32))
    Image.fromarray(np.full((4, 4), 255, dtype=np.uint8), mode="L").save(out_dir / "change_mask.png")
    (out_dir / "run_metadata.json").write_text(
        json.dumps(
            {
                "device_resolved": "mps",
                "allow_mps_fallback": False,
                "pytorch_enable_mps_fallback": None,
                "mps_built": True,
                "mps_available": True,
                "mps_test_cfg": {"applied": False},
                "stage_timings": {
                    "run_id": "bandon:outputs",
                    "stages": [
                        {
                            "name": "forward",
                            "duration_ms": 12.5,
                            "status": "success",
                            "metadata": {"device": "mps"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        captured.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("src.domain.bandon_runner.shutil.which", lambda command: None)
    monkeypatch.setattr("src.domain.bandon_runner._run_command", fake_run)
    result = run_bandon_inference(
        image_a_path=image_a,
        image_b_path=image_b,
        settings=settings,
        out_dir=out_dir,
    )
    assert captured
    assert "--device" in captured[0]
    assert "mps" in captured[0]
    assert result.change_probability.shape == (4, 4)
    assert result.change_mask.dtype == bool
    assert result.child_timing is not None
    assert result.child_timing["stages"][0]["name"] == "forward"


def test_run_bandon_inference_raises_on_failure(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(image_a)
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(image_b)

    def fake_run(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(args=command, returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr("src.domain.bandon_runner.shutil.which", lambda command: None)
    monkeypatch.setattr("src.domain.bandon_runner._run_command", fake_run)
    try:
        run_bandon_inference(
            image_a_path=image_a,
            image_b_path=image_b,
            settings=settings,
            out_dir=out_dir,
        )
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("Expected BANDON inference to raise on non-zero exit.")


def test_resolve_launcher_prefers_env_python_over_conda(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    env_python = settings.bandon_env_prefix / "bin" / "python"
    monkeypatch.setattr("src.domain.bandon_runner.shutil.which", lambda command: "/opt/homebrew/bin/conda")
    launcher_name, launcher_prefix, python_executable = _resolve_launcher(settings.bandon_env_prefix)
    assert launcher_name == "env_python"
    assert launcher_prefix == [str(env_python)]
    assert python_executable == str(env_python)
