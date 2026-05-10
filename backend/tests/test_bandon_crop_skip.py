from __future__ import annotations

from pathlib import Path
import subprocess

import numpy as np
from PIL import Image

from src.config import Settings
from src.domain.bandon_crop_skip import should_skip_crop
from src.domain import bandon_runner


def test_outside_aoi_crop_skipped() -> None:
    decision = should_skip_crop(
        np.zeros((4, 4), dtype=bool),
        np.ones((4, 4), dtype=bool),
        0.01,
    )

    assert decision.skip is True
    assert decision.reason == "outside_aoi"
    assert decision.aoi_pixels == 0


def test_nodata_crop_inside_aoi_skipped() -> None:
    decision = should_skip_crop(
        np.ones((4, 4), dtype=bool),
        np.zeros((4, 4), dtype=bool),
        0.01,
    )

    assert decision.skip is True
    assert decision.reason == "no_valid_paired_imagery_inside_aoi"
    assert decision.valid_inside_aoi_pixels == 0


def test_low_valid_ratio_crop_inside_aoi_skipped() -> None:
    aoi = np.ones((10, 10), dtype=bool)
    valid = np.zeros((10, 10), dtype=bool)
    valid[0, 0] = True

    decision = should_skip_crop(aoi, valid, 0.02)

    assert decision.skip is True
    assert decision.reason == "low_valid_paired_imagery_inside_aoi"
    assert decision.valid_ratio_within_aoi == 0.01


def test_valid_crop_forwarded() -> None:
    aoi = np.ones((10, 10), dtype=bool)
    valid = np.zeros((10, 10), dtype=bool)
    valid[:5, :] = True

    decision = should_skip_crop(aoi, valid, 0.01)

    assert decision.skip is False
    assert decision.reason is None


def test_skip_disabled_forwards_all_crops() -> None:
    decision = should_skip_crop(
        np.zeros((4, 4), dtype=bool),
        np.zeros((4, 4), dtype=bool),
        0.01,
        skip_outside_aoi=False,
        skip_nodata=False,
    )

    assert decision.skip is False
    assert decision.reason is None


def test_mask_shape_mismatch_fails_clearly() -> None:
    try:
        should_skip_crop(np.ones((4, 4), dtype=bool), np.ones((5, 4), dtype=bool), 0.01)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("Expected shape mismatch to fail.")


def _write_rgb(path: Path, shape: tuple[int, int] = (4, 4)) -> None:
    Image.fromarray(np.zeros((*shape, 3), dtype=np.uint8)).save(path)


def _write_mask(path: Path, shape: tuple[int, int] = (4, 4)) -> None:
    Image.fromarray(np.ones(shape, dtype=np.uint8) * 255).save(path)


def test_bandon_runner_passes_crop_skip_masks_when_available(monkeypatch, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    env_prefix = tmp_path / "env"
    runner_path = repo_dir / "tools" / "infer_mps.py"
    config_path = repo_dir / "config.py"
    checkpoint_path = repo_dir / "checkpoint.pth"
    runner_path.parent.mkdir(parents=True)
    env_prefix.mkdir()
    for path in (runner_path, config_path, checkpoint_path):
        path.write_text("", encoding="utf-8")

    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    t1_mask = tmp_path / "t1_mask.png"
    t2_mask = tmp_path / "t2_mask.png"
    aoi_mask = tmp_path / "aoi_mask.png"
    _write_rgb(image_a)
    _write_rgb(image_b)
    _write_mask(t1_mask)
    _write_mask(t2_mask)
    _write_mask(aoi_mask)

    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        bandon_repo_dir=repo_dir,
        bandon_env_prefix=env_prefix,
        bandon_config_path=config_path,
        bandon_checkpoint_path=checkpoint_path,
        bandon_skip_invalid_crops=True,
    )
    captured: dict[str, list[str]] = {}

    def fake_resolve_launcher(_env_prefix: Path):
        return "test_launcher", ["python"], "python"

    def fake_run_command(command: list[str], *, env: dict[str, str]):
        captured["command"] = command
        outdir = Path(command[command.index("--outdir") + 1])
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "run_metadata.json").write_text(
            '{"device_resolved":"mps","allow_mps_fallback":false}',
            encoding="utf-8",
        )
        np.save(outdir / "change_probability.npy", np.zeros((4, 4), dtype=np.float32))
        Image.fromarray(np.zeros((4, 4), dtype=np.uint8)).save(outdir / "change_mask.png")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(bandon_runner, "_resolve_launcher", fake_resolve_launcher)
    monkeypatch.setattr(bandon_runner, "_run_command", fake_run_command)

    bandon_runner.run_bandon_inference(
        image_a_path=image_a,
        image_b_path=image_b,
        settings=settings,
        out_dir=tmp_path / "out",
        t1_valid_mask_path=t1_mask,
        t2_valid_mask_path=t2_mask,
        aoi_mask_path=aoi_mask,
    )

    command = captured["command"]
    assert "--skip-invalid-crops" in command
    assert command[command.index("--t1-valid-mask") + 1] == str(t1_mask)
    assert command[command.index("--t2-valid-mask") + 1] == str(t2_mask)
    assert command[command.index("--aoi-mask") + 1] == str(aoi_mask)
    assert command[command.index("--min-valid-ratio-within-aoi") + 1] == "0.01"


def test_bandon_runner_omits_crop_skip_masks_when_missing(monkeypatch, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    env_prefix = tmp_path / "env"
    runner_path = repo_dir / "tools" / "infer_mps.py"
    config_path = repo_dir / "config.py"
    checkpoint_path = repo_dir / "checkpoint.pth"
    runner_path.parent.mkdir(parents=True)
    env_prefix.mkdir()
    for path in (runner_path, config_path, checkpoint_path):
        path.write_text("", encoding="utf-8")

    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    _write_rgb(image_a)
    _write_rgb(image_b)

    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        bandon_repo_dir=repo_dir,
        bandon_env_prefix=env_prefix,
        bandon_config_path=config_path,
        bandon_checkpoint_path=checkpoint_path,
        bandon_skip_invalid_crops=True,
    )
    captured: dict[str, list[str]] = {}

    def fake_resolve_launcher(_env_prefix: Path):
        return "test_launcher", ["python"], "python"

    def fake_run_command(command: list[str], *, env: dict[str, str]):
        captured["command"] = command
        outdir = Path(command[command.index("--outdir") + 1])
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "run_metadata.json").write_text(
            '{"device_resolved":"mps","allow_mps_fallback":false}',
            encoding="utf-8",
        )
        np.save(outdir / "change_probability.npy", np.zeros((4, 4), dtype=np.float32))
        Image.fromarray(np.zeros((4, 4), dtype=np.uint8)).save(outdir / "change_mask.png")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(bandon_runner, "_resolve_launcher", fake_resolve_launcher)
    monkeypatch.setattr(bandon_runner, "_run_command", fake_run_command)

    bandon_runner.run_bandon_inference(
        image_a_path=image_a,
        image_b_path=image_b,
        settings=settings,
        out_dir=tmp_path / "out",
        t1_valid_mask_path=None,
        t2_valid_mask_path=None,
        aoi_mask_path=None,
    )

    command = captured["command"]
    assert "--skip-invalid-crops" not in command
    assert "--t1-valid-mask" not in command
    assert "--t2-valid-mask" not in command
    assert "--aoi-mask" not in command
