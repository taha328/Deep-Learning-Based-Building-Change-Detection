from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import subprocess
import threading
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from src.config import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BandonRuntimeProbe:
    available: bool
    message: str
    launcher: str | None = None
    python_executable: str | None = None
    repo_dir: str | None = None
    env_prefix: str | None = None
    runner_path: str | None = None
    config_path: str | None = None
    checkpoint_path: str | None = None
    device_requested: str | None = None
    torch_version: str | None = None
    mmcv_version: str | None = None
    mps_built: bool | None = None
    mps_available: bool | None = None

    def diagnostics(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for key in (
            "launcher",
            "python_executable",
            "repo_dir",
            "env_prefix",
            "runner_path",
            "config_path",
            "checkpoint_path",
            "device_requested",
            "torch_version",
            "mmcv_version",
        ):
            value = getattr(self, key)
            if value:
                values[key] = value
        if self.mps_built is not None:
            values["mps_built"] = str(self.mps_built).lower()
        if self.mps_available is not None:
            values["mps_available"] = str(self.mps_available).lower()
        return values


@dataclass(frozen=True)
class BandonRunResult:
    change_probability: np.ndarray
    change_mask: np.ndarray
    metadata: dict[str, Any]
    child_timing: dict[str, Any] | None
    stdout: str
    stderr: str
    command: list[str]
    launcher: str


def _resolve_runtime_paths(settings: Settings) -> dict[str, Path]:
    repo_dir = settings.bandon_repo_dir.expanduser().resolve()
    env_prefix = settings.bandon_env_prefix.expanduser().resolve()
    config_path = settings.bandon_config_path.expanduser()
    if not config_path.is_absolute():
        config_path = (repo_dir / config_path).resolve()
    else:
        config_path = config_path.resolve()
    checkpoint_path = settings.bandon_checkpoint_path.expanduser()
    if not checkpoint_path.is_absolute():
        checkpoint_path = (settings.project_root / checkpoint_path).resolve()
    else:
        checkpoint_path = checkpoint_path.resolve()
    runner_path = repo_dir / "tools" / "infer_mps.py"
    return {
        "repo_dir": repo_dir,
        "env_prefix": env_prefix,
        "config_path": config_path,
        "checkpoint_path": checkpoint_path,
        "runner_path": runner_path,
    }


def _resolve_launcher(env_prefix: Path) -> tuple[str, list[str], str]:
    env_python = env_prefix / "bin" / "python"
    if env_python.exists():
        return ("env_python", [str(env_python)], str(env_python))

    conda_executable = shutil.which("conda")
    if conda_executable:
        return (
            "conda_run",
            [conda_executable, "run", "-p", str(env_prefix), "python"],
            "python",
        )

    raise RuntimeError(
        f"Unable to locate a BANDON launcher for env prefix {env_prefix}. "
        "Neither `conda` on PATH nor env/bin/python is available."
    )


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTORCH_ENABLE_MPS_FALLBACK", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_pipe_thread(fd: int, chunks: list[bytes]) -> None:
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return


def _run_command(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if sys.platform != "darwin" or not hasattr(os, "posix_spawn"):
        return subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_thread = threading.Thread(target=_read_pipe_thread, args=(stdout_read, stdout_chunks), daemon=True)
    stderr_thread = threading.Thread(target=_read_pipe_thread, args=(stderr_read, stderr_chunks), daemon=True)

    file_actions = [
        (os.POSIX_SPAWN_DUP2, stdout_write, 1),
        (os.POSIX_SPAWN_DUP2, stderr_write, 2),
        (os.POSIX_SPAWN_CLOSE, stdout_read),
        (os.POSIX_SPAWN_CLOSE, stderr_read),
        (os.POSIX_SPAWN_CLOSE, stdout_write),
        (os.POSIX_SPAWN_CLOSE, stderr_write),
    ]

    try:
        pid = os.posix_spawn(command[0], command, env, file_actions=file_actions)
    finally:
        os.close(stdout_write)
        os.close(stderr_write)

    stdout_thread.start()
    stderr_thread.start()
    _pid, status = os.waitpid(pid, 0)
    stdout_thread.join()
    stderr_thread.join()

    return subprocess.CompletedProcess(
        args=command,
        returncode=os.waitstatus_to_exitcode(status),
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


def _probe_command(settings: Settings) -> tuple[list[str], str, dict[str, Path]]:
    paths = _resolve_runtime_paths(settings)
    launcher_name, launcher_prefix, python_executable = _resolve_launcher(paths["env_prefix"])
    probe_code = (
        "import json, mmcv, torch; "
        "print(json.dumps({"
        "'python_executable': __import__(\"sys\").executable, "
        "'torch_version': torch.__version__, "
        "'mmcv_version': mmcv.__version__, "
        "'mps_built': bool(hasattr(torch.backends, \"mps\") and torch.backends.mps.is_built()), "
        "'mps_available': bool(hasattr(torch.backends, \"mps\") and torch.backends.mps.is_available())"
        "}))"
    )
    return launcher_prefix + ["-c", probe_code], python_executable, paths


def probe_bandon_runtime(settings: Settings) -> BandonRuntimeProbe:
    try:
        command, python_executable, paths = _probe_command(settings)
    except Exception as exc:
        return BandonRuntimeProbe(available=False, message=str(exc))

    missing_paths = [
        (label, path)
        for label, path in paths.items()
        if label != "env_prefix" and not path.exists()
    ]
    if not paths["repo_dir"].exists():
        missing_paths.insert(0, ("repo_dir", paths["repo_dir"]))
    if not paths["env_prefix"].exists():
        missing_paths.insert(0, ("env_prefix", paths["env_prefix"]))
    if missing_paths:
        missing_text = ", ".join(f"{label}={path}" for label, path in missing_paths)
        return BandonRuntimeProbe(
            available=False,
            message=f"BANDON runtime is incomplete: {missing_text}",
            launcher=command[0],
            python_executable=python_executable,
            repo_dir=str(paths["repo_dir"]),
            env_prefix=str(paths["env_prefix"]),
            runner_path=str(paths["runner_path"]),
            config_path=str(paths["config_path"]),
            checkpoint_path=str(paths["checkpoint_path"]),
            device_requested=settings.bandon_device,
        )

    completed = _run_command(
        command,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        return BandonRuntimeProbe(
            available=False,
            message=f"BANDON runtime probe failed: {error_text}",
            launcher=command[0],
            python_executable=python_executable,
            repo_dir=str(paths["repo_dir"]),
            env_prefix=str(paths["env_prefix"]),
            runner_path=str(paths["runner_path"]),
            config_path=str(paths["config_path"]),
            checkpoint_path=str(paths["checkpoint_path"]),
            device_requested=settings.bandon_device,
        )

    stdout_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(stdout_lines[-1])
    except Exception as exc:
        return BandonRuntimeProbe(
            available=False,
            message=f"BANDON runtime probe returned invalid JSON: {exc}",
            launcher=command[0],
            python_executable=python_executable,
            repo_dir=str(paths["repo_dir"]),
            env_prefix=str(paths["env_prefix"]),
            runner_path=str(paths["runner_path"]),
            config_path=str(paths["config_path"]),
            checkpoint_path=str(paths["checkpoint_path"]),
            device_requested=settings.bandon_device,
        )

    mps_available = bool(payload.get("mps_available"))
    if settings.bandon_device == "mps" and not mps_available:
        return BandonRuntimeProbe(
            available=False,
            message="BANDON MPS was requested, but torch.backends.mps.is_available() is false in the BANDON environment.",
            launcher=command[0],
            python_executable=str(payload.get("python_executable") or python_executable),
            repo_dir=str(paths["repo_dir"]),
            env_prefix=str(paths["env_prefix"]),
            runner_path=str(paths["runner_path"]),
            config_path=str(paths["config_path"]),
            checkpoint_path=str(paths["checkpoint_path"]),
            device_requested=settings.bandon_device,
            torch_version=str(payload.get("torch_version") or ""),
            mmcv_version=str(payload.get("mmcv_version") or ""),
            mps_built=bool(payload.get("mps_built")),
            mps_available=mps_available,
        )

    return BandonRuntimeProbe(
        available=True,
        message=(
            f"BANDON MTGCDNet runtime is available via {command[0]} on {settings.bandon_device}. "
            f"torch={payload.get('torch_version')} mmcv={payload.get('mmcv_version')}"
        ),
        launcher=command[0],
        python_executable=str(payload.get("python_executable") or python_executable),
        repo_dir=str(paths["repo_dir"]),
        env_prefix=str(paths["env_prefix"]),
        runner_path=str(paths["runner_path"]),
        config_path=str(paths["config_path"]),
        checkpoint_path=str(paths["checkpoint_path"]),
        device_requested=settings.bandon_device,
        torch_version=str(payload.get("torch_version") or ""),
        mmcv_version=str(payload.get("mmcv_version") or ""),
        mps_built=bool(payload.get("mps_built")),
        mps_available=mps_available,
    )


def run_bandon_inference(
    *,
    image_a_path: Path,
    image_b_path: Path,
    settings: Settings,
    out_dir: Path,
    t1_valid_mask_path: Path | None = None,
    t2_valid_mask_path: Path | None = None,
    aoi_mask_path: Path | None = None,
    effective_backend: str | None = None,
    threshold: float | None = None,
) -> BandonRunResult:
    paths = _resolve_runtime_paths(settings)
    launcher_name, launcher_prefix, _python_executable = _resolve_launcher(paths["env_prefix"])
    command = launcher_prefix + [
        str(paths["runner_path"]),
        "--config",
        str(paths["config_path"]),
        "--checkpoint",
        str(paths["checkpoint_path"]),
        "--image-a",
        str(image_a_path),
        "--image-b",
        str(image_b_path),
        "--device",
        settings.bandon_device,
        "--outdir",
        str(out_dir),
        "--effective-backend",
        effective_backend or settings.inference_backend,
    ]
    if settings.bandon_allow_mps_fallback:
        command.append("--allow-mps-fallback")
    skip_mask_paths = (t1_valid_mask_path, t2_valid_mask_path, aoi_mask_path)
    if settings.bandon_skip_invalid_crops:
        if all(path is not None and path.exists() for path in skip_mask_paths):
            logger.info(
                "BANDON_CROP_SKIP_CLI_ENABLED t1Valid=%s t2Valid=%s aoi=%s minValidRatioWithinAoi=%s",
                t1_valid_mask_path,
                t2_valid_mask_path,
                aoi_mask_path,
                settings.bandon_min_valid_ratio_within_aoi,
            )
            command.extend(
                [
                    "--skip-invalid-crops",
                    "--t1-valid-mask",
                    str(t1_valid_mask_path),
                    "--t2-valid-mask",
                    str(t2_valid_mask_path),
                    "--aoi-mask",
                    str(aoi_mask_path),
                    "--min-valid-ratio-within-aoi",
                    str(settings.bandon_min_valid_ratio_within_aoi),
                ]
            )
            if settings.bandon_skip_outside_aoi_crops:
                command.append("--skip-outside-aoi-crops")
            if settings.bandon_skip_nodata_crops:
                command.append("--skip-nodata-crops")
        else:
            logger.warning(
                "BANDON_CROP_SKIP_CLI_DISABLED reason=missing_masks t1=%s t2=%s aoi=%s",
                t1_valid_mask_path,
                t2_valid_mask_path,
                aoi_mask_path,
            )
    else:
        logger.info("BANDON_CROP_SKIP_CLI_DISABLED reason=setting_disabled")

    completed = _run_command(
        command,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(
            "BANDON MTGCDNet inference failed with exit code "
            f"{completed.returncode}: {error_text}"
        )

    metadata_path = out_dir / "run_metadata.json"
    probability_path = out_dir / "change_probability.npy"
    mask_path = out_dir / "change_mask.png"
    if not metadata_path.exists():
        raise RuntimeError(f"BANDON MTGCDNet did not write run_metadata.json to {metadata_path}")
    if not probability_path.exists():
        raise RuntimeError(f"BANDON MTGCDNet did not write change_probability.npy to {probability_path}")
    if not mask_path.exists():
        raise RuntimeError(f"BANDON MTGCDNet did not write change_mask.png to {mask_path}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        {
            "effective_backend": effective_backend or settings.inference_backend,
            "runner_family": metadata.get("runner_family") or "bandon_mps",
            "checkpoint_path": str(paths["checkpoint_path"]),
            "checkpoint_sha256": _sha256_file(paths["checkpoint_path"]),
            "threshold": threshold,
            "device": settings.bandon_device,
            "config_path": str(paths["config_path"]),
            "normalization_used": metadata.get("normalization_used")
            or {
                "type": "mmcv.Normalize",
                "name": "app_0_1",
                "mean": [0.0, 0.0, 0.0],
                "std": [255.0, 255.0, 255.0],
                "to_rgb": True,
            },
            "input_t1": str(image_a_path),
            "input_t2": str(image_b_path),
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    child_timing = metadata.get("stage_timings") if isinstance(metadata.get("stage_timings"), dict) else None
    change_probability = np.load(probability_path).astype(np.float32)
    change_mask = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8) > 0

    device_resolved = str(metadata.get("device_resolved") or "")
    if settings.bandon_device == "mps" and device_resolved != "mps":
        raise RuntimeError(
            f"BANDON MTGCDNet resolved device '{device_resolved}' instead of native mps."
        )
    if not settings.bandon_allow_mps_fallback:
        if bool(metadata.get("allow_mps_fallback")):
            raise RuntimeError("BANDON MTGCDNet unexpectedly enabled allow_mps_fallback.")
        if metadata.get("pytorch_enable_mps_fallback"):
            raise RuntimeError(
                "BANDON MTGCDNet unexpectedly ran with PYTORCH_ENABLE_MPS_FALLBACK enabled."
            )

    return BandonRunResult(
        change_probability=change_probability,
        change_mask=change_mask,
        metadata=metadata,
        child_timing=child_timing,
        stdout=completed.stdout,
        stderr=completed.stderr,
        command=command,
        launcher=launcher_name,
    )
