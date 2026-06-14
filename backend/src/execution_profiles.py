from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.config import Settings, resolve_inference_checkpoint
from src.domain.bandon_runner import probe_bandon_runtime


InferenceBackendName = Literal["bandon_mps"]
BackendProbeMode = InferenceBackendName
ModelDeviceName = Literal["auto", "cpu", "cuda", "mps"]


def _sha256_file_or_none(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PipelineExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inference_backend: InferenceBackendName = "bandon_mps"


class BackendAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: BackendProbeMode
    label: str
    available: bool
    enabled_by_default: bool
    reason: str | None = None
    diagnostics: dict[str, str] = Field(default_factory=dict)


class InferenceRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: InferenceBackendName
    checkpoint_env_var: str
    checkpoint_path: Path
    change_threshold: float
    semantic_threshold: float
    threshold_source: Literal["backend_settings_env"] = "backend_settings_env"
    device: ModelDeviceName
    repo_dir: Path
    config_path: Path

    def diagnostics(self) -> dict[str, str]:
        return {
            "inference_backend_requested": self.backend,
            "inference_backend_resolved": self.backend,
            "checkpoint_env_var_used": self.checkpoint_env_var,
            "checkpoint_path_resolved": str(self.checkpoint_path),
            "checkpoint_exists": str(self.checkpoint_path.is_file()).lower(),
            "checkpoint_size_bytes": str(self.checkpoint_path.stat().st_size),
            "change_threshold": str(self.change_threshold),
            "semantic_threshold": str(self.semantic_threshold),
            "threshold_source": self.threshold_source,
            "device_requested": self.device,
        }


class LocalInferenceBackend:
    model_backend: Literal["bandon_mps"] = "bandon_mps"

    def __init__(self, execution_config: PipelineExecutionConfig) -> None:
        self.execution_config = execution_config
        self.probe_mode: InferenceBackendName = execution_config.inference_backend
        self.label = "BANDON (Local)"

    def configure_settings(self, settings: Settings) -> Settings:
        return settings.model_copy(update={"inference_backend": self.execution_config.inference_backend})

    def availability(self, settings: Settings) -> BackendAvailability:
        configured = self.configure_settings(settings)
        try:
            runtime = resolve_inference_runtime(configured)
        except (RuntimeError, ValueError) as exc:
            return BackendAvailability(
                mode=self.probe_mode,
                label=self.label,
                available=False,
                enabled_by_default=configured.inference_backend == self.probe_mode,
                reason=str(exc),
            )
        probe = probe_bandon_runtime(configured)
        diagnostics = runtime.diagnostics()
        diagnostics.update(probe.diagnostics())
        return BackendAvailability(
            mode=self.probe_mode,
            label=self.label,
            available=probe.available,
            enabled_by_default=configured.inference_backend == self.probe_mode,
            reason=None if probe.available else probe.message,
            diagnostics=diagnostics,
        )

    def enforce_remote_patch_budget(self) -> bool:
        return False

    def request_hash_context(self, settings: Settings) -> dict[str, object]:
        configured = self.configure_settings(settings)
        runtime = resolve_inference_runtime(configured)
        checkpoint_path = runtime.checkpoint_path.expanduser().resolve()
        config_path = runtime.config_path.expanduser()
        if not config_path.is_absolute():
            config_path = (runtime.repo_dir / config_path).resolve()
        else:
            config_path = config_path.resolve()
        return {
            "model_backend": self.model_backend,
            "inference_backend": runtime.backend,
            "inference_backend_requested": runtime.backend,
            "inference_backend_resolved": runtime.backend,
            "bandon_processing_version": 4,
            "bandon_repo_dir": str(runtime.repo_dir),
            "bandon_config_path": str(config_path),
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_env_var_used": runtime.checkpoint_env_var,
            "checkpoint_path_resolved": str(checkpoint_path),
            "checkpoint_exists": checkpoint_path.is_file(),
            "checkpoint_size_bytes": checkpoint_path.stat().st_size,
            "checkpoint_sha256": _sha256_file_or_none(checkpoint_path) or "",
            "device": runtime.device,
            "device_requested": runtime.device,
            "change_threshold": runtime.change_threshold,
            "threshold_source": runtime.threshold_source,
        }

    def create_inference_runner(self, settings: Settings):
        del settings
        return None


def resolve_inference_runtime(settings: Settings) -> InferenceRuntimeConfig:
    checkpoint = resolve_inference_checkpoint(settings)
    return InferenceRuntimeConfig(
        backend=checkpoint.backend,
        checkpoint_env_var=checkpoint.env_var,
        checkpoint_path=checkpoint.path,
        change_threshold=settings.change_threshold,
        semantic_threshold=settings.semantic_threshold,
        device=settings.bandon_device,
        repo_dir=settings.bandon_repo_dir,
        config_path=settings.bandon_config_path,
    )


def resolve_backend(
    execution_config: PipelineExecutionConfig | None = None,
    *,
    settings: Settings | None = None,
) -> LocalInferenceBackend:
    if execution_config is None:
        backend = settings.inference_backend if settings is not None else "bandon_mps"
        execution_config = PipelineExecutionConfig(inference_backend=backend)
    return LocalInferenceBackend(execution_config)


def resolve_configured_inference_execution_config(settings: Settings) -> PipelineExecutionConfig:
    return PipelineExecutionConfig(inference_backend=settings.inference_backend)


def collect_backend_availability(
    *,
    settings: Settings,
    execution_config: PipelineExecutionConfig | None = None,
) -> list[BackendAvailability]:
    if execution_config is not None:
        return [resolve_backend(execution_config, settings=settings).availability(settings)]
    return [resolve_backend(PipelineExecutionConfig(inference_backend="bandon_mps"), settings=settings).availability(settings)]
