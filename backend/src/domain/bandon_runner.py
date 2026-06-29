from __future__ import annotations

import atexit
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
import time
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

from src.domain.inference_timing import elapsed_ms

if TYPE_CHECKING:
    from src.config import Settings


logger = logging.getLogger(__name__)
_CHECKPOINT_SHA_CACHE_LOCK = threading.Lock()
_CHECKPOINT_SHA_CACHE: dict[tuple[str, int, int], str] = {}


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
    device_resolved: str | None = None
    torch_version: str | None = None
    mmcv_version: str | None = None
    cuda_available: bool | None = None
    cuda_device_count: int | None = None
    cuda_device_name: str | None = None
    torch_cuda_version: str | None = None
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
            "device_resolved",
            "torch_version",
            "mmcv_version",
            "cuda_device_name",
            "torch_cuda_version",
        ):
            value = getattr(self, key)
            if value is not None:
                values[key] = value
        if self.cuda_available is not None:
            values["cuda_available"] = str(self.cuda_available).lower()
        if self.cuda_device_count is not None:
            values["cuda_device_count"] = str(self.cuda_device_count)
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
    parent_timing_ms: dict[str, Any] | None = None


def _resolve_runtime_paths(settings: Settings) -> dict[str, Path]:
    from src.config import resolve_inference_checkpoint

    repo_dir = settings.bandon_repo_dir.expanduser().resolve()
    env_prefix = settings.bandon_env_prefix.expanduser().resolve()
    config_path = settings.bandon_config_path.expanduser()
    if not config_path.is_absolute():
        config_path = (repo_dir / config_path).resolve()
    else:
        config_path = config_path.resolve()
    checkpoint_path = resolve_inference_checkpoint(settings).path
    runner_path = repo_dir / "tools" / "infer_mps.py"
    return {
        "repo_dir": repo_dir,
        "env_prefix": env_prefix,
        "config_path": config_path,
        "checkpoint_path": checkpoint_path,
        "runner_path": runner_path,
    }


def _resolve_launcher(env_prefix: Path) -> tuple[str, list[str], str]:
    env_python_candidates = (
        env_prefix / "bin" / "python",
        env_prefix / "Scripts" / "python.exe",
        env_prefix / "Scripts" / "python",
    )
    for env_python in env_python_candidates:
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
        "Neither `conda` on PATH nor env/bin/python or env/Scripts/python.exe is available."
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


def _checkpoint_sha256(path: Path) -> str:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    key = (str(resolved), int(stat.st_size), int(stat.st_mtime_ns))
    with _CHECKPOINT_SHA_CACHE_LOCK:
        cached = _CHECKPOINT_SHA_CACHE.get(key)
    if cached is not None:
        return cached
    digest = _sha256_file(resolved)
    with _CHECKPOINT_SHA_CACHE_LOCK:
        _CHECKPOINT_SHA_CACHE.clear()
        _CHECKPOINT_SHA_CACHE[key] = digest
    return digest


def _process_rss_mb(pid: int) -> float | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout.strip()
    if not raw:
        return None
    try:
        return round(float(raw.splitlines()[-1].strip()) / 1024.0, 3)
    except ValueError:
        return None


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
    _launcher_name, launcher_prefix, python_executable = _resolve_launcher(paths["env_prefix"])
    probe_code = (
        "import json, mmcv, sys, torch; "
        f"requested = {settings.bandon_device!r}; "
        "cuda_available = bool(torch.cuda.is_available()); "
        "mps_available = bool(hasattr(torch.backends, \"mps\") and torch.backends.mps.is_available()); "
        "device_resolved = ("
        "'cpu' if requested == 'cpu' else "
        "('cuda' if requested == 'cuda' and cuda_available else "
        "('mps' if requested == 'mps' and sys.platform == 'darwin' and mps_available else "
        "(('cuda' if cuda_available else ('mps' if sys.platform == 'darwin' and mps_available else 'cpu')) "
        "if requested == 'auto' else None)))); "
        "print(json.dumps({"
        "'python_executable': __import__(\"sys\").executable, "
        "'torch_version': torch.__version__, "
        "'mmcv_version': mmcv.__version__, "
        "'device_requested': requested, "
        "'device_resolved': device_resolved, "
        "'cuda_available': cuda_available, "
        "'cuda_device_count': int(torch.cuda.device_count()) if cuda_available else 0, "
        "'cuda_device_name': torch.cuda.get_device_name(0) if cuda_available else None, "
        "'torch_cuda_version': torch.version.cuda, "
        "'mps_built': bool(hasattr(torch.backends, \"mps\") and torch.backends.mps.is_built()), "
        "'mps_available': mps_available"
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
    cuda_available = bool(payload.get("cuda_available"))
    cuda_device_count = int(payload.get("cuda_device_count") or 0)
    device_resolved = payload.get("device_resolved")
    if settings.bandon_device == "mps" and device_resolved != "mps":
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
            device_resolved=str(device_resolved or ""),
            torch_version=str(payload.get("torch_version") or ""),
            mmcv_version=str(payload.get("mmcv_version") or ""),
            cuda_available=cuda_available,
            cuda_device_count=cuda_device_count,
            cuda_device_name=payload.get("cuda_device_name"),
            torch_cuda_version=payload.get("torch_cuda_version"),
            mps_built=bool(payload.get("mps_built")),
            mps_available=mps_available,
        )
    if settings.bandon_device == "cuda" and device_resolved != "cuda":
        return BandonRuntimeProbe(
            available=False,
            message="BANDON CUDA was requested, but torch.cuda.is_available() is false in the BANDON environment.",
            launcher=command[0],
            python_executable=str(payload.get("python_executable") or python_executable),
            repo_dir=str(paths["repo_dir"]),
            env_prefix=str(paths["env_prefix"]),
            runner_path=str(paths["runner_path"]),
            config_path=str(paths["config_path"]),
            checkpoint_path=str(paths["checkpoint_path"]),
            device_requested=settings.bandon_device,
            device_resolved=str(device_resolved or ""),
            torch_version=str(payload.get("torch_version") or ""),
            mmcv_version=str(payload.get("mmcv_version") or ""),
            cuda_available=cuda_available,
            cuda_device_count=cuda_device_count,
            cuda_device_name=payload.get("cuda_device_name"),
            torch_cuda_version=payload.get("torch_cuda_version"),
            mps_built=bool(payload.get("mps_built")),
            mps_available=mps_available,
        )

    return BandonRuntimeProbe(
        available=True,
        message=(
            f"BANDON runtime is available via {command[0]} on {settings.bandon_device}"
            f" resolved to {device_resolved}. "
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
        device_resolved=str(device_resolved or ""),
        torch_version=str(payload.get("torch_version") or ""),
        mmcv_version=str(payload.get("mmcv_version") or ""),
        cuda_available=cuda_available,
        cuda_device_count=cuda_device_count,
        cuda_device_name=payload.get("cuda_device_name"),
        torch_cuda_version=payload.get("torch_cuda_version"),
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
    timing_enabled = bool(getattr(settings, "inference_timing_enabled", False))
    command_prepare_started = time.perf_counter()
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

    command_prepare_ms = elapsed_ms(command_prepare_started)
    command_env = _clean_env()
    if timing_enabled:
        command_env["APP_INFERENCE_TIMING_ENABLED"] = "1"

    subprocess_started = time.perf_counter()
    completed = _run_command(
        command,
        env=command_env,
    )
    subprocess_wall_ms = elapsed_ms(subprocess_started)
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(
            "BANDON inference failed with exit code "
            f"{completed.returncode}: {error_text}"
        )

    metadata_path = out_dir / "run_metadata.json"
    probability_path = out_dir / "change_probability.npy"
    mask_path = out_dir / "change_mask.png"
    if not metadata_path.exists():
        raise RuntimeError(f"BANDON did not write run_metadata.json to {metadata_path}")
    if not probability_path.exists():
        raise RuntimeError(f"BANDON did not write change_probability.npy to {probability_path}")
    if not mask_path.exists():
        raise RuntimeError(f"BANDON did not write change_mask.png to {mask_path}")

    metadata_read_started = time.perf_counter()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    result_metadata_read_ms = elapsed_ms(metadata_read_started)
    checkpoint_sha_started = time.perf_counter()
    checkpoint_sha256 = _checkpoint_sha256(paths["checkpoint_path"])
    parent_checkpoint_sha_ms = elapsed_ms(checkpoint_sha_started)
    output_read_started = time.perf_counter()
    change_probability = np.load(probability_path).astype(np.float32)
    change_mask = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8) > 0
    bandon_output_read_ms = elapsed_ms(output_read_started)
    runner_timing_ms = {
        "subprocess_command_prepare_ms": command_prepare_ms,
        "subprocess_wall_ms": subprocess_wall_ms,
        "subprocess_return_code": int(completed.returncode),
        "subprocess_stdout_bytes": len(completed.stdout.encode("utf-8", errors="replace")),
        "subprocess_stderr_bytes": len(completed.stderr.encode("utf-8", errors="replace")),
        "parent_checkpoint_sha_ms": parent_checkpoint_sha_ms,
        "result_metadata_read_ms": result_metadata_read_ms,
        "bandon_output_read_ms": bandon_output_read_ms,
    }
    metadata.update(
        {
            "effective_backend": effective_backend or settings.inference_backend,
            "runner_family": metadata.get("runner_family") or "bandon_mps",
            "checkpoint_path": str(paths["checkpoint_path"]),
            "checkpoint_sha256": checkpoint_sha256,
            "threshold": threshold,
            "device": settings.bandon_device,
            "device_requested": metadata.get("device_requested") or settings.bandon_device,
            "device_resolved": metadata.get("device_resolved"),
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
    if timing_enabled:
        metadata["bandon_runner_timing_ms"] = runner_timing_ms
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    child_timing = metadata.get("stage_timings") if isinstance(metadata.get("stage_timings"), dict) else None

    device_resolved = str(metadata.get("device_resolved") or "")
    if settings.bandon_device in {"cuda", "mps"} and device_resolved != settings.bandon_device:
        raise RuntimeError(
            f"BANDON resolved device '{device_resolved}' instead of requested {settings.bandon_device}."
        )
    if not settings.bandon_allow_mps_fallback:
        if bool(metadata.get("allow_mps_fallback")):
            raise RuntimeError("BANDON unexpectedly enabled allow_mps_fallback.")
        if metadata.get("pytorch_enable_mps_fallback"):
            raise RuntimeError(
                "BANDON unexpectedly ran with PYTORCH_ENABLE_MPS_FALLBACK enabled."
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
        parent_timing_ms=runner_timing_ms if timing_enabled else None,
    )


class PersistentBandonRunner:
    """One BANDON model process reused across many tile predictions."""

    def __init__(
        self,
        *,
        settings: Settings,
        effective_backend: str | None = None,
        threshold: float | None = None,
    ) -> None:
        self._settings = settings
        self._effective_backend = effective_backend or settings.inference_backend
        self._threshold = threshold
        self._timing_enabled = bool(getattr(settings, "inference_timing_enabled", False))
        self._paths = _resolve_runtime_paths(settings)
        self._launcher_name, launcher_prefix, _python_executable = _resolve_launcher(self._paths["env_prefix"])
        self._worker_path = self._paths["repo_dir"] / "tools" / "persistent_infer_worker.py"
        if not self._worker_path.exists():
            raise RuntimeError(f"BANDON persistent worker is missing: {self._worker_path}")
        self._command = launcher_prefix + [
            str(self._worker_path),
            "--config",
            str(self._paths["config_path"]),
            "--checkpoint",
            str(self._paths["checkpoint_path"]),
            "--device",
            settings.bandon_device,
            "--effective-backend",
            self._effective_backend,
        ]
        if settings.bandon_allow_mps_fallback:
            self._command.append("--allow-mps-fallback")
        self._stderr_chunks: list[bytes] = []
        env = _clean_env()
        if self._timing_enabled:
            env["APP_INFERENCE_TIMING_ENABLED"] = "1"
        startup_started = time.perf_counter()
        self._process = subprocess.Popen(
            self._command,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        if self._process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=_read_pipe_thread,
                args=(self._process.stderr.fileno(), self._stderr_chunks),
                daemon=True,
            )
            self._stderr_thread.start()
        else:
            self._stderr_thread = None
        atexit.register(self.close)
        ready = self._read_response()
        self._startup_wall_ms = elapsed_ms(startup_started)
        if ready.get("status") != "ready":
            self.close()
            error = ready.get("error") or self._stderr_tail() or "unknown persistent worker startup error"
            raise RuntimeError(f"BANDON persistent worker failed to start: {error}")
        self._ready = ready
        self._predict_count = 0

    @property
    def model_load_count(self) -> int:
        value = self._ready.get("model_load_count")
        return int(value) if isinstance(value, (int, float)) else 1

    @property
    def command(self) -> list[str]:
        return list(self._command)

    @property
    def launcher(self) -> str:
        return self._launcher_name

    def _stderr_tail(self) -> str:
        data = b"".join(self._stderr_chunks)
        if not data:
            return ""
        return data[-8000:].decode("utf-8", errors="replace")

    def _read_response(self) -> dict[str, Any]:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("BANDON persistent worker stdout pipe is unavailable.")
        while True:
            line = stdout.readline()
            if not line:
                returncode = self._process.poll()
                if returncode is not None:
                    raise RuntimeError(
                        "BANDON persistent worker exited before response "
                        f"(code {returncode}): {self._stderr_tail()}"
                    )
                continue
            try:
                payload = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload

    def _send(self, payload: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise RuntimeError("BANDON persistent worker stdin pipe is unavailable.")
        stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        stdin.flush()

    def predict_tile(
        self,
        *,
        image_a_path: Path,
        image_b_path: Path,
        out_dir: Path,
        t1_valid_mask_path: Path | None = None,
        t2_valid_mask_path: Path | None = None,
        aoi_mask_path: Path | None = None,
    ) -> BandonRunResult:
        if self._process.poll() is not None:
            raise RuntimeError(
                "BANDON persistent worker is not running "
                f"(code {self._process.returncode}): {self._stderr_tail()}"
            )
        skip_mask_paths = (t1_valid_mask_path, t2_valid_mask_path, aoi_mask_path)
        skip_invalid_crops = bool(
            self._settings.bandon_skip_invalid_crops
            and all(path is not None and path.exists() for path in skip_mask_paths)
        )
        if self._settings.bandon_skip_invalid_crops and not skip_invalid_crops:
            logger.warning(
                "BANDON_CROP_SKIP_PERSISTENT_DISABLED reason=missing_masks t1=%s t2=%s aoi=%s",
                t1_valid_mask_path,
                t2_valid_mask_path,
                aoi_mask_path,
            )
        request = {
            "command": "predict",
            "image_a": str(image_a_path),
            "image_b": str(image_b_path),
            "outdir": str(out_dir),
            "skip_invalid_crops": skip_invalid_crops,
            "t1_valid_mask": str(t1_valid_mask_path) if t1_valid_mask_path is not None else None,
            "t2_valid_mask": str(t2_valid_mask_path) if t2_valid_mask_path is not None else None,
            "aoi_mask": str(aoi_mask_path) if aoi_mask_path is not None else None,
            "skip_outside_aoi_crops": bool(self._settings.bandon_skip_outside_aoi_crops),
            "skip_nodata_crops": bool(self._settings.bandon_skip_nodata_crops),
            "min_valid_ratio_within_aoi": float(self._settings.bandon_min_valid_ratio_within_aoi),
            "threshold": self._threshold,
        }
        request_started = time.perf_counter()
        self._send(request)
        response = self._read_response()
        persistent_request_ms = elapsed_ms(request_started)
        if response.get("status") != "ok":
            error = response.get("error") or self._stderr_tail() or "unknown persistent worker error"
            raise RuntimeError(f"BANDON persistent inference failed: {error}")

        metadata_path = out_dir / "run_metadata.json"
        probability_path = out_dir / "change_probability.npy"
        mask_path = out_dir / "change_mask.png"
        if not metadata_path.exists():
            raise RuntimeError(f"BANDON persistent worker did not write run_metadata.json to {metadata_path}")
        if not probability_path.exists():
            raise RuntimeError(f"BANDON persistent worker did not write change_probability.npy to {probability_path}")
        if not mask_path.exists():
            raise RuntimeError(f"BANDON persistent worker did not write change_mask.png to {mask_path}")

        metadata_read_started = time.perf_counter()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        result_metadata_read_ms = elapsed_ms(metadata_read_started)
        checkpoint_sha_started = time.perf_counter()
        checkpoint_sha256 = _checkpoint_sha256(self._paths["checkpoint_path"])
        parent_checkpoint_sha_ms = elapsed_ms(checkpoint_sha_started)
        output_read_started = time.perf_counter()
        change_probability = np.load(probability_path).astype(np.float32)
        change_mask = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8) > 0
        bandon_output_read_ms = elapsed_ms(output_read_started)
        self._predict_count += 1
        runner_timing_ms = {
            "persistent_worker_request_ms": persistent_request_ms,
            "persistent_worker_startup_wall_ms": self._startup_wall_ms if self._predict_count == 1 else 0.0,
            "persistent_worker_pid": int(self._process.pid),
            "persistent_worker_rss_mb": _process_rss_mb(int(self._process.pid)),
            "subprocess_command_prepare_ms": 0.0,
            "subprocess_wall_ms": 0.0,
            "subprocess_return_code": None,
            "subprocess_stdout_bytes": 0,
            "subprocess_stderr_bytes": len(b"".join(self._stderr_chunks)),
            "parent_checkpoint_sha_ms": parent_checkpoint_sha_ms,
            "result_metadata_read_ms": result_metadata_read_ms,
            "bandon_output_read_ms": bandon_output_read_ms,
        }
        metadata.update(
            {
                "effective_backend": self._effective_backend,
                "runner_family": metadata.get("runner_family") or "bandon_mps",
                "bandon_inference_mode": "persistent_runner",
                "checkpoint_path": str(self._paths["checkpoint_path"]),
                "checkpoint_sha256": checkpoint_sha256,
                "threshold": self._threshold,
                "device": self._settings.bandon_device,
                "device_requested": metadata.get("device_requested") or self._settings.bandon_device,
                "device_resolved": metadata.get("device_resolved"),
                "config_path": str(self._paths["config_path"]),
                "input_t1": str(image_a_path),
                "input_t2": str(image_b_path),
                "model_load_count_total": self.model_load_count,
            }
        )
        if self._timing_enabled:
            metadata["bandon_runner_timing_ms"] = runner_timing_ms
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        child_timing = metadata.get("stage_timings") if isinstance(metadata.get("stage_timings"), dict) else None

        device_resolved = str(metadata.get("device_resolved") or "")
        if self._settings.bandon_device in {"cuda", "mps"} and device_resolved != self._settings.bandon_device:
            raise RuntimeError(
                f"BANDON resolved device '{device_resolved}' instead of requested {self._settings.bandon_device}."
            )
        if not self._settings.bandon_allow_mps_fallback:
            if bool(metadata.get("allow_mps_fallback")):
                raise RuntimeError("BANDON unexpectedly enabled allow_mps_fallback.")
            if metadata.get("pytorch_enable_mps_fallback"):
                raise RuntimeError(
                    "BANDON unexpectedly ran with PYTORCH_ENABLE_MPS_FALLBACK enabled."
                )

        return BandonRunResult(
            change_probability=change_probability,
            change_mask=change_mask,
            metadata=metadata,
            child_timing=child_timing,
            stdout="",
            stderr=self._stderr_tail(),
            command=self.command,
            launcher=self.launcher,
            parent_timing_ms=runner_timing_ms if self._timing_enabled else None,
        )

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None or process.poll() is not None:
            return
        try:
            self._send({"command": "shutdown"})
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
