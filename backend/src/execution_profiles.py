from __future__ import annotations

from abc import ABC, abstractmethod
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.config import Settings
from src.domain.bandon_runner import probe_bandon_runtime
from src.domain.inference import run_single_scene_inference, run_tiled_inference
from src.domain.local_inference import LocalInferenceConfig, probe_local_runtime, run_local_single_scene_inference, run_local_tiled_inference


ModelBackendMode = Literal["sam3", "bandon_mps"]
SegmentationBackendMode = Literal["public_zerogpu", "local", "huggingface_gpu"]
BackendProbeMode = Literal["public_zerogpu", "local", "huggingface_gpu", "bandon_mps"]


class PublicZeroGpuBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spaces: list[str] = Field(default_factory=list)
    api_name: str | None = None
    prompt: str | None = None
    hf_token: str | None = None


class LocalBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_path: str | None = None
    device_preference: Literal["auto", "cuda", "mps", "cpu"] = "auto"
    prompt: str = "building"


class HuggingFaceGpuBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spaces: list[str] = Field(default_factory=list)
    api_name: str | None = None
    prompt: str | None = None
    hf_token: str | None = None


class BandonBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str | None = None
    env_prefix: str | None = None
    config_path: str | None = None
    checkpoint_path: str | None = None
    device: Literal["mps", "cpu"] = "mps"
    allow_mps_fallback: bool = False


class PipelineExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_backend: ModelBackendMode = "sam3"
    backend_mode: SegmentationBackendMode = "public_zerogpu"
    public_zerogpu: PublicZeroGpuBackendConfig = Field(default_factory=PublicZeroGpuBackendConfig)
    local: LocalBackendConfig = Field(default_factory=LocalBackendConfig)
    huggingface_gpu: HuggingFaceGpuBackendConfig = Field(default_factory=HuggingFaceGpuBackendConfig)
    bandon_mps: BandonBackendConfig = Field(default_factory=BandonBackendConfig)


class BackendAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: BackendProbeMode
    label: str
    available: bool
    enabled_by_default: bool
    reason: str | None = None
    diagnostics: dict[str, str] = Field(default_factory=dict)


class DetectionBackend(ABC):
    model_backend: ModelBackendMode
    probe_mode: BackendProbeMode
    label: str

    def __init__(self, execution_config: PipelineExecutionConfig) -> None:
        self.execution_config = execution_config

    @abstractmethod
    def availability(self, settings: Settings) -> BackendAvailability:
        raise NotImplementedError

    def enabled_by_default(self, settings: Settings) -> bool:
        return settings.model_backend_default == self.model_backend and self.probe_mode == "public_zerogpu"

    def enforce_remote_patch_budget(self) -> bool:
        return self.probe_mode not in {"local", "bandon_mps"}

    def configure_settings(self, settings: Settings) -> Settings:
        return settings

    def request_hash_context(self, settings: Settings) -> dict[str, object]:
        del settings
        return {
            "model_backend": self.model_backend,
            "backend_mode": self.probe_mode,
        }

    def create_inference_runner(self, settings: Settings):
        del settings
        return None

    def create_segmentation_runner(self, settings: Settings):
        del settings
        return None


class PublicSam3ZeroGpuBackend(DetectionBackend):
    model_backend: ModelBackendMode = "sam3"
    probe_mode: BackendProbeMode = "public_zerogpu"
    label = "Public Hugging Face ZeroGPU"

    def availability(self, settings: Settings) -> BackendAvailability:
        spaces = self.execution_config.public_zerogpu.spaces or list(settings.remote_segmentation_spaces)
        available = len(spaces) > 0
        return BackendAvailability(
            mode=self.probe_mode,
            label=self.label,
            available=available,
            enabled_by_default=self.enabled_by_default(settings),
            reason=None if available else "No public SAM3 Spaces are configured.",
            diagnostics={"provider_count": str(len(spaces))},
        )

    def configure_settings(self, settings: Settings) -> Settings:
        config = self.execution_config.public_zerogpu
        spaces = tuple(config.spaces) if config.spaces else settings.remote_segmentation_spaces
        primary_space = spaces[0] if spaces else settings.remote_segmentation_space
        return settings.model_copy(
            update={
                "remote_segmentation_space": primary_space,
                "remote_segmentation_spaces": spaces,
                "remote_segmentation_api_name": config.api_name or settings.remote_segmentation_api_name,
                "remote_segmentation_prompt": config.prompt or settings.remote_segmentation_prompt,
                "remote_segmentation_hf_token": config.hf_token or settings.remote_segmentation_hf_token,
            }
        )

    def request_hash_context(self, settings: Settings) -> dict[str, object]:
        configured = self.configure_settings(settings)
        return {
            "model_backend": self.model_backend,
            "backend_mode": self.probe_mode,
            "remote_segmentation_spaces": list(configured.remote_segmentation_spaces),
            "remote_segmentation_api_name": configured.remote_segmentation_api_name,
            "remote_segmentation_prompt": configured.remote_segmentation_prompt,
        }

    def create_inference_runner(self, settings: Settings):
        del settings
        return run_tiled_inference

    def create_segmentation_runner(self, settings: Settings):
        del settings
        return run_single_scene_inference


class LocalInferenceBackend(DetectionBackend):
    model_backend: ModelBackendMode = "sam3"
    probe_mode: BackendProbeMode = "local"
    label = "Local SAM3"

    def availability(self, settings: Settings) -> BackendAvailability:
        del settings
        probe = probe_local_runtime(
            LocalInferenceConfig(
                checkpoint_path=self.execution_config.local.checkpoint_path,
                device_preference=self.execution_config.local.device_preference,
                prompt=self.execution_config.local.prompt,
            )
        )
        diagnostics: dict[str, str] = {}
        if probe.device is not None:
            diagnostics["device"] = probe.device
        if probe.implementation is not None:
            diagnostics["implementation"] = probe.implementation
        if probe.model_source is not None:
            diagnostics["model_source"] = probe.model_source
        return BackendAvailability(
            mode=self.probe_mode,
            label=self.label,
            available=probe.available,
            enabled_by_default=False,
            reason=None if probe.available else probe.message,
            diagnostics=diagnostics,
        )

    def request_hash_context(self, settings: Settings) -> dict[str, object]:
        del settings
        return {
            "model_backend": self.model_backend,
            "backend_mode": self.probe_mode,
            "local_checkpoint_path": self.execution_config.local.checkpoint_path or "",
            "local_device_preference": self.execution_config.local.device_preference,
            "local_prompt": self.execution_config.local.prompt,
        }

    def create_inference_runner(self, settings: Settings):
        del settings
        return partial(
            run_local_tiled_inference,
            local_config=LocalInferenceConfig(
                checkpoint_path=self.execution_config.local.checkpoint_path,
                device_preference=self.execution_config.local.device_preference,
                prompt=self.execution_config.local.prompt,
            ),
        )

    def create_segmentation_runner(self, settings: Settings):
        del settings
        return partial(
            run_local_single_scene_inference,
            local_config=LocalInferenceConfig(
                checkpoint_path=self.execution_config.local.checkpoint_path,
                device_preference=self.execution_config.local.device_preference,
                prompt=self.execution_config.local.prompt,
            ),
        )


class HuggingFaceGpuBackend(DetectionBackend):
    model_backend: ModelBackendMode = "sam3"
    probe_mode: BackendProbeMode = "huggingface_gpu"
    label = "Configured Hugging Face GPU"

    def availability(self, settings: Settings) -> BackendAvailability:
        del settings
        spaces = self.execution_config.huggingface_gpu.spaces
        available = len(spaces) > 0
        return BackendAvailability(
            mode=self.probe_mode,
            label=self.label,
            available=available,
            enabled_by_default=False,
            reason=None if available else "No Hugging Face GPU Space or endpoint is configured.",
            diagnostics={"provider_count": str(len(spaces))},
        )

    def configure_settings(self, settings: Settings) -> Settings:
        config = self.execution_config.huggingface_gpu
        spaces = tuple(config.spaces)
        primary_space = spaces[0] if spaces else settings.remote_segmentation_space
        return settings.model_copy(
            update={
                "remote_segmentation_space": primary_space,
                "remote_segmentation_spaces": spaces,
                "remote_segmentation_api_name": config.api_name or settings.remote_segmentation_api_name,
                "remote_segmentation_prompt": config.prompt or settings.remote_segmentation_prompt,
                "remote_segmentation_hf_token": config.hf_token or settings.remote_segmentation_hf_token,
            }
        )

    def request_hash_context(self, settings: Settings) -> dict[str, object]:
        configured = self.configure_settings(settings)
        return {
            "model_backend": self.model_backend,
            "backend_mode": self.probe_mode,
            "remote_segmentation_spaces": list(configured.remote_segmentation_spaces),
            "remote_segmentation_api_name": configured.remote_segmentation_api_name,
            "remote_segmentation_prompt": configured.remote_segmentation_prompt,
        }

    def create_inference_runner(self, settings: Settings):
        del settings
        return run_tiled_inference

    def create_segmentation_runner(self, settings: Settings):
        del settings
        return run_single_scene_inference


class BandonMpsDetectionBackend(DetectionBackend):
    model_backend: ModelBackendMode = "bandon_mps"
    probe_mode: BackendProbeMode = "bandon_mps"
    label = "BANDON MTGCDNet (Local MPS)"

    def enabled_by_default(self, settings: Settings) -> bool:
        return settings.model_backend_default == self.model_backend

    def configure_settings(self, settings: Settings) -> Settings:
        config = self.execution_config.bandon_mps
        update: dict[str, object] = {
            "bandon_device": config.device or settings.bandon_device,
            "bandon_allow_mps_fallback": bool(config.allow_mps_fallback),
        }
        if config.repo_dir:
            update["bandon_repo_dir"] = Path(config.repo_dir).expanduser()
        if config.env_prefix:
            update["bandon_env_prefix"] = Path(config.env_prefix).expanduser()
        if config.config_path:
            update["bandon_config_path"] = Path(config.config_path).expanduser()
        if config.checkpoint_path:
            update["bandon_checkpoint_path"] = Path(config.checkpoint_path).expanduser()
        if settings.inference_backend == "mtgcdnet_s2looking_mps":
            if settings.s2looking_checkpoint_path is None:
                raise RuntimeError(
                    "APP_S2LOOKING_CHECKPOINT_PATH is required when "
                    "APP_INFERENCE_BACKEND=mtgcdnet_s2looking_mps."
                )
            update["bandon_checkpoint_path"] = settings.s2looking_checkpoint_path
            update["default_change_threshold"] = settings.s2looking_change_threshold
        return settings.model_copy(update=update)

    def availability(self, settings: Settings) -> BackendAvailability:
        configured = self.configure_settings(settings)
        probe = probe_bandon_runtime(configured)
        return BackendAvailability(
            mode=self.probe_mode,
            label=self.label,
            available=probe.available,
            enabled_by_default=self.enabled_by_default(settings),
            reason=None if probe.available else probe.message,
            diagnostics=probe.diagnostics(),
        )

    def request_hash_context(self, settings: Settings) -> dict[str, object]:
        configured = self.configure_settings(settings)
        return {
            "model_backend": self.model_backend,
            "backend_mode": self.probe_mode,
            "effective_backend": configured.inference_backend,
            "bandon_processing_version": 3,
            "bandon_repo_dir": str(configured.bandon_repo_dir),
            "bandon_env_prefix": str(configured.bandon_env_prefix),
            "bandon_config_path": str(configured.bandon_config_path),
            "bandon_checkpoint_path": str(configured.bandon_checkpoint_path),
            "bandon_device": configured.bandon_device,
            "bandon_allow_mps_fallback": configured.bandon_allow_mps_fallback,
            "change_threshold": configured.default_change_threshold,
        }


def resolve_backend(
    execution_config: PipelineExecutionConfig | None = None,
    *,
    settings: Settings | None = None,
) -> DetectionBackend:
    if execution_config is None:
        default_model_backend = settings.model_backend_default if settings is not None else "sam3"
        config = PipelineExecutionConfig(model_backend=default_model_backend)
    else:
        config = execution_config

    if config.model_backend == "bandon_mps":
        return BandonMpsDetectionBackend(config)
    if config.backend_mode == "public_zerogpu":
        return PublicSam3ZeroGpuBackend(config)
    if config.backend_mode == "local":
        return LocalInferenceBackend(config)
    if config.backend_mode == "huggingface_gpu":
        return HuggingFaceGpuBackend(config)
    raise RuntimeError(f"Unsupported backend mode: {config.backend_mode}")


def resolve_configured_inference_execution_config(settings: Settings) -> PipelineExecutionConfig:
    if settings.inference_backend in {"bandon_mps", "mtgcdnet_s2looking_mps"}:
        return PipelineExecutionConfig(model_backend="bandon_mps")
    raise RuntimeError(f"Unsupported inference backend: {settings.inference_backend}")


def collect_backend_availability(
    *,
    settings: Settings,
    execution_config: PipelineExecutionConfig | None = None,
) -> list[BackendAvailability]:
    config = execution_config or PipelineExecutionConfig(model_backend=settings.model_backend_default)
    variants = [
        config.model_copy(update={"model_backend": "sam3", "backend_mode": "public_zerogpu"}),
        config.model_copy(update={"model_backend": "sam3", "backend_mode": "local"}),
        config.model_copy(update={"model_backend": "sam3", "backend_mode": "huggingface_gpu"}),
        config.model_copy(update={"model_backend": "bandon_mps"}),
    ]
    return [resolve_backend(variant, settings=settings).availability(settings) for variant in variants]
