from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import platform
import time
from typing import Callable, Literal

import numpy as np
from PIL import Image

from src.config import Settings
from src.domain.inference import InferenceDiagnostics, derive_change_probability
from src.domain.tiling import iter_patch_windows, pad_patch_rgb
from src.utils.logging import get_logger


LOGGER = get_logger(__name__)

LocalDevicePreference = Literal["auto", "cuda", "mps", "cpu"]
LocalRuntimeImplementation = Literal["official_repo", "transformers"]


@dataclass(frozen=True)
class LocalRuntimeProbe:
    available: bool
    message: str
    device: str | None = None
    implementation: LocalRuntimeImplementation | None = None
    model_source: str | None = None


@dataclass(frozen=True)
class LocalInferenceConfig:
    checkpoint_path: str | None = None
    device_preference: LocalDevicePreference = "auto"
    prompt: str = "building"


def _import_torch():
    import torch

    return torch


def detect_local_device(preference: LocalDevicePreference) -> str:
    torch = _import_torch()
    cuda_available = torch.cuda.is_available()
    mps_available = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())

    if preference == "auto":
        if cuda_available:
            return "cuda"
        if mps_available:
            return "mps"
        return "cpu"
    if preference == "cuda":
        if not cuda_available:
            raise RuntimeError("CUDA was requested, but torch.cuda.is_available() is false.")
        return "cuda"
    if preference == "mps":
        if not mps_available:
            raise RuntimeError("MPS was requested, but torch.backends.mps.is_available() is false.")
        return "mps"
    if preference == "cpu":
        return "cpu"
    raise RuntimeError(f"Unsupported local device preference: {preference}")


def _preferred_runtime_order(config: LocalInferenceConfig) -> tuple[LocalRuntimeImplementation, ...]:
    try:
        device = detect_local_device(config.device_preference)
    except RuntimeError:
        return ("official_repo", "transformers")
    if platform.system() == "Darwin" and device == "mps":
        return ("transformers", "official_repo")
    return ("official_repo", "transformers")


def _normalize_local_source(raw: str | None) -> str | None:
    if not raw:
        return None
    path = Path(raw)
    if path.exists():
        return str(path.resolve())
    return raw


def _looks_like_local_transformers_dir(path: Path) -> bool:
    return path.is_dir() and (path / "config.json").exists()


def _probe_repo_runtime(config: LocalInferenceConfig) -> LocalRuntimeProbe:
    try:
        from sam3.model.sam3_image_processor import Sam3Processor  # noqa: F401
        from sam3.model_builder import build_sam3_image_model  # noqa: F401
    except Exception as exc:
        return LocalRuntimeProbe(
            available=False,
            message=(
                "Official facebookresearch/sam3 runtime is unavailable in the backend environment. "
                f"Import failed: {type(exc).__name__}: {exc}"
            ),
        )

    try:
        device = detect_local_device(config.device_preference)
    except RuntimeError as exc:
        return LocalRuntimeProbe(available=False, message=str(exc))

    checkpoint_path = config.checkpoint_path
    if checkpoint_path:
        checkpoint = Path(checkpoint_path)
        if not checkpoint.exists():
            return LocalRuntimeProbe(
                available=False,
                message=f"Configured local SAM3 checkpoint path does not exist: {checkpoint_path}",
            )
        if checkpoint.is_dir():
            return LocalRuntimeProbe(
                available=False,
                message=(
                    "Official facebookresearch/sam3 runtime expects a checkpoint file (.pt) or "
                    "default HF download path, not a model directory."
                ),
                device=device,
            )

    return LocalRuntimeProbe(
        available=True,
        device=device,
        implementation="official_repo",
        model_source=str(Path(checkpoint_path).resolve()) if checkpoint_path else "facebook/sam3",
        message=(
            f"Local SAM3 runtime detected on {device}. "
            + (
                f"Checkpoint path configured: {checkpoint_path}"
                if checkpoint_path
                else "No checkpoint path configured; runtime will rely on SAM3's default HF download path."
            )
        ),
    )


def _probe_transformers_runtime(config: LocalInferenceConfig) -> LocalRuntimeProbe:
    try:
        from transformers import Sam3Model  # noqa: F401
        from transformers import Sam3Processor  # noqa: F401
    except Exception as exc:
        return LocalRuntimeProbe(
            available=False,
            message=(
                "Official Transformers SAM3 runtime is unavailable in the backend environment. "
                f"Import failed: {type(exc).__name__}: {exc}"
            ),
        )

    try:
        device = detect_local_device(config.device_preference)
    except RuntimeError as exc:
        return LocalRuntimeProbe(available=False, message=str(exc))

    model_source = _normalize_local_source(config.checkpoint_path) or "facebook/sam3"
    source_path = Path(model_source)
    if source_path.exists() and source_path.is_file():
        return LocalRuntimeProbe(
            available=False,
            message=(
                "Transformers SAM3 expects a model directory or a Hub model id. "
                f"Got checkpoint file path: {model_source}"
            ),
            device=device,
        )
    if source_path.exists() and not _looks_like_local_transformers_dir(source_path):
        return LocalRuntimeProbe(
            available=False,
            message=(
                "Configured local SAM3 directory is missing config.json and cannot be loaded by "
                f"Transformers: {model_source}"
            ),
            device=device,
        )

    return LocalRuntimeProbe(
        available=True,
        device=device,
        implementation="transformers",
        model_source=model_source,
        message=(
            f"Official Transformers SAM3 runtime detected on {device}. "
            f"Model source: {model_source}"
        ),
    )


def probe_local_runtime(config: LocalInferenceConfig) -> LocalRuntimeProbe:
    probe_results = {
        "official_repo": _probe_repo_runtime(config),
        "transformers": _probe_transformers_runtime(config),
    }
    ordered = _preferred_runtime_order(config)

    for implementation in ordered:
        probe = probe_results[implementation]
        if probe.available:
            if implementation == "transformers" and platform.system() == "Darwin" and probe.device == "mps":
                repo_probe = probe_results["official_repo"]
                return LocalRuntimeProbe(
                    available=True,
                    message=(
                        f"{probe.message}. "
                        "On Apple Silicon, the official Hugging Face Transformers SAM3 implementation "
                        "is the preferred runtime path. "
                        + (
                            f"Meta repo runtime status: {repo_probe.message}"
                            if not repo_probe.available
                            else "Meta repo runtime is also available."
                        )
                    ),
                    device=probe.device,
                    implementation=probe.implementation,
                    model_source=probe.model_source,
                )
            if implementation != ordered[0]:
                primary_probe = probe_results[ordered[0]]
                return LocalRuntimeProbe(
                    available=True,
                    message=(
                        f"{probe.message}. "
                        f"Preferred runtime on this machine was unavailable: {primary_probe.message}"
                    ),
                    device=probe.device,
                    implementation=probe.implementation,
                    model_source=probe.model_source,
                )
            return probe

    first = probe_results[ordered[0]]
    second = probe_results[ordered[1]]
    return LocalRuntimeProbe(
        available=False,
        message=(
            "Local SAM3 is unavailable. "
            f"{ordered[0]} runtime: {first.message}. "
            f"{ordered[1]} runtime: {second.message}"
        ),
        device=first.device or second.device,
    )


@lru_cache(maxsize=4)
def _load_repo_processor(
    checkpoint_path: str | None,
    device: str,
    semantic_threshold: float,
):
    from sam3.model.sam3_image_processor import Sam3Processor
    from sam3.model_builder import build_sam3_image_model

    model = build_sam3_image_model(
        device=device,
        checkpoint_path=checkpoint_path,
        eval_mode=True,
        load_from_HF=True,
        enable_segmentation=True,
        enable_inst_interactivity=False,
    )
    return Sam3Processor(
        model,
        device=device,
        confidence_threshold=float(semantic_threshold),
    )


@lru_cache(maxsize=2)
def _load_transformers_model(
    model_source: str,
    device: str,
):
    from transformers import Sam3Model, Sam3Processor

    model = Sam3Model.from_pretrained(model_source)
    model = model.to(device)
    model.eval()
    processor = Sam3Processor.from_pretrained(model_source)
    return model, processor


def _predict_repo_patch_mask(
    patch_rgb: np.ndarray,
    *,
    processor,
    prompt: str,
    semantic_threshold: float,
) -> tuple[np.ndarray, float, float]:
    prepare_start = time.perf_counter()
    image = Image.fromarray(patch_rgb)
    prepare_seconds = time.perf_counter() - prepare_start

    infer_start = time.perf_counter()
    state = processor.set_image(image)
    processor.set_confidence_threshold(float(semantic_threshold), state)
    state = processor.set_text_prompt(prompt, state)
    infer_seconds = time.perf_counter() - infer_start

    masks = state.get("masks")
    if masks is None:
        raise RuntimeError("Local SAM3 inference did not return any masks.")

    if hasattr(masks, "detach"):
        masks = masks.detach().cpu().numpy()
    masks = np.asarray(masks, dtype=bool)
    if masks.ndim == 4:
        masks = masks[:, 0, :, :]
    if masks.ndim == 3:
        combined = np.any(masks, axis=0)
    elif masks.ndim == 2:
        combined = masks
    else:
        raise RuntimeError(f"Unexpected local SAM3 mask shape: {masks.shape}")
    return combined.astype(bool), prepare_seconds, infer_seconds


def _predict_transformers_patch_mask(
    patch_rgb: np.ndarray,
    *,
    model,
    processor,
    device: str,
    prompt: str,
    semantic_threshold: float,
) -> tuple[np.ndarray, float, float]:
    prepare_start = time.perf_counter()
    image = Image.fromarray(patch_rgb)
    inputs = processor(images=image, text=prompt, return_tensors="pt")
    prepared_inputs = {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in inputs.items()
    }
    prepare_seconds = time.perf_counter() - prepare_start

    infer_start = time.perf_counter()
    torch = _import_torch()
    with torch.no_grad():
        outputs = model(**prepared_inputs)
    results = processor.post_process_instance_segmentation(
        outputs,
        threshold=float(semantic_threshold),
        mask_threshold=float(semantic_threshold),
        target_sizes=[image.size[::-1]],
    )
    infer_seconds = time.perf_counter() - infer_start

    if not results:
        return np.zeros(patch_rgb.shape[:2], dtype=bool), prepare_seconds, infer_seconds
    masks = results[0].get("masks")
    if masks is None:
        return np.zeros(patch_rgb.shape[:2], dtype=bool), prepare_seconds, infer_seconds
    if hasattr(masks, "detach"):
        masks = masks.detach().cpu().numpy()
    masks = np.asarray(masks)
    if masks.size == 0:
        return np.zeros(patch_rgb.shape[:2], dtype=bool), prepare_seconds, infer_seconds
    if masks.ndim == 2:
        combined = masks
    elif masks.ndim == 3:
        combined = np.any(masks > 0, axis=0)
    else:
        raise RuntimeError(f"Unexpected Transformers SAM3 mask shape: {masks.shape}")
    return combined.astype(bool), prepare_seconds, infer_seconds


def _run_repo_scene_segmentation(
    scene_rgb: np.ndarray,
    *,
    scene_label: str,
    settings: Settings,
    semantic_threshold: float,
    local_config: LocalInferenceConfig,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[np.ndarray, InferenceDiagnostics]:
    probe = _probe_repo_runtime(local_config)
    if not probe.available or probe.device is None:
        raise RuntimeError(probe.message)

    processor = _load_repo_processor(
        local_config.checkpoint_path,
        probe.device,
        float(semantic_threshold),
    )

    height, width = scene_rgb.shape[:2]
    windows = list(iter_patch_windows(height, width, settings.patch_size, settings.stride))
    score_sum = np.zeros((height, width), dtype=np.float32)
    counts = np.zeros((height, width), dtype=np.float32)

    patch_prepare_seconds = 0.0
    inference_seconds = 0.0

    for patch_index, window in enumerate(windows, start=1):
        patch = pad_patch_rgb(
            scene_rgb[window.y0 : window.y1, window.x0 : window.x1].copy(),
            settings.patch_size,
        )
        mask, patch_prepare, infer_elapsed = _predict_repo_patch_mask(
            patch,
            processor=processor,
            prompt=local_config.prompt or settings.remote_segmentation_prompt,
            semantic_threshold=semantic_threshold,
        )
        patch_prepare_seconds += patch_prepare
        inference_seconds += infer_elapsed
        orig_h = window.y1 - window.y0
        orig_w = window.x1 - window.x0
        score_sum[window.y0 : window.y1, window.x0 : window.x1] += mask[:orig_h, :orig_w].astype(np.float32)
        counts[window.y0 : window.y1, window.x0 : window.x1] += 1.0

        if progress_callback:
            progress_callback(
                f"Segmented {scene_label} patch progress {patch_index}/{len(windows)} "
                f"via local SAM3 ({probe.device})"
            )

    return (
        score_sum / np.maximum(counts, 1.0),
        InferenceDiagnostics(
            patch_count=len(windows),
            patch_prepare_seconds=round(patch_prepare_seconds, 4),
            remote_seconds=round(inference_seconds, 4),
            mask_decode_seconds=0.0,
        ),
    )


def _run_transformers_scene_segmentation(
    scene_rgb: np.ndarray,
    *,
    scene_label: str,
    settings: Settings,
    semantic_threshold: float,
    local_config: LocalInferenceConfig,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[np.ndarray, InferenceDiagnostics]:
    probe = _probe_transformers_runtime(local_config)
    if not probe.available or probe.device is None or not probe.model_source:
        raise RuntimeError(probe.message)

    model, processor = _load_transformers_model(
        probe.model_source,
        probe.device,
    )

    height, width = scene_rgb.shape[:2]
    windows = list(iter_patch_windows(height, width, settings.patch_size, settings.stride))
    score_sum = np.zeros((height, width), dtype=np.float32)
    counts = np.zeros((height, width), dtype=np.float32)

    patch_prepare_seconds = 0.0
    inference_seconds = 0.0

    for patch_index, window in enumerate(windows, start=1):
        patch = pad_patch_rgb(
            scene_rgb[window.y0 : window.y1, window.x0 : window.x1].copy(),
            settings.patch_size,
        )
        mask, patch_prepare, infer_elapsed = _predict_transformers_patch_mask(
            patch,
            model=model,
            processor=processor,
            device=probe.device,
            prompt=local_config.prompt or settings.remote_segmentation_prompt,
            semantic_threshold=semantic_threshold,
        )
        patch_prepare_seconds += patch_prepare
        inference_seconds += infer_elapsed
        orig_h = window.y1 - window.y0
        orig_w = window.x1 - window.x0
        score_sum[window.y0 : window.y1, window.x0 : window.x1] += mask[:orig_h, :orig_w].astype(np.float32)
        counts[window.y0 : window.y1, window.x0 : window.x1] += 1.0

        if progress_callback:
            progress_callback(
                f"Segmented {scene_label} patch progress {patch_index}/{len(windows)} "
                f"via local SAM3 Transformers ({probe.device})"
            )

    return (
        score_sum / np.maximum(counts, 1.0),
        InferenceDiagnostics(
            patch_count=len(windows),
            patch_prepare_seconds=round(patch_prepare_seconds, 4),
            remote_seconds=round(inference_seconds, 4),
            mask_decode_seconds=0.0,
        ),
    )


def run_local_tiled_inference(
    arr_t1: np.ndarray,
    arr_t2: np.ndarray,
    *,
    settings: Settings,
    semantic_threshold: float,
    local_config: LocalInferenceConfig,
    cache_dir=None,
    x_ip_token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, np.ndarray], InferenceDiagnostics]:
    del cache_dir, x_ip_token

    if arr_t1.shape != arr_t2.shape:
        raise ValueError("T1 and T2 arrays must have identical shapes before inference.")

    probe = probe_local_runtime(local_config)
    if not probe.available or probe.implementation is None:
        raise RuntimeError(probe.message)

    scene_runner = (
        _run_repo_scene_segmentation
        if probe.implementation == "official_repo"
        else _run_transformers_scene_segmentation
    )

    t1_prediction, t1_diag = scene_runner(
        arr_t1,
        scene_label="t1",
        settings=settings,
        semantic_threshold=semantic_threshold,
        local_config=local_config,
        progress_callback=progress_callback,
    )
    t2_prediction, t2_diag = scene_runner(
        arr_t2,
        scene_label="t2",
        settings=settings,
        semantic_threshold=semantic_threshold,
        local_config=local_config,
        progress_callback=progress_callback,
    )
    change_prediction = derive_change_probability(t1_prediction, t2_prediction)

    return (
        {
            "t1_semantic_prediction": t1_prediction,
            "t2_semantic_prediction": t2_prediction,
            "change_prediction": change_prediction,
        },
        InferenceDiagnostics(
            patch_count=t1_diag.patch_count + t2_diag.patch_count,
            patch_prepare_seconds=round(
                t1_diag.patch_prepare_seconds + t2_diag.patch_prepare_seconds,
                4,
            ),
            remote_seconds=round(t1_diag.remote_seconds + t2_diag.remote_seconds, 4),
            mask_decode_seconds=round(
                t1_diag.mask_decode_seconds + t2_diag.mask_decode_seconds,
                4,
            ),
        ),
    )


def run_local_single_scene_inference(
    scene_rgb: np.ndarray,
    *,
    settings: Settings,
    semantic_threshold: float,
    local_config: LocalInferenceConfig,
    cache_dir=None,
    x_ip_token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, np.ndarray], InferenceDiagnostics]:
    del cache_dir, x_ip_token

    probe = probe_local_runtime(local_config)
    if not probe.available or probe.implementation is None:
        raise RuntimeError(probe.message)

    scene_runner = (
        _run_repo_scene_segmentation
        if probe.implementation == "official_repo"
        else _run_transformers_scene_segmentation
    )
    prediction, diagnostics = scene_runner(
        scene_rgb,
        scene_label="source",
        settings=settings,
        semantic_threshold=semantic_threshold,
        local_config=local_config,
        progress_callback=progress_callback,
    )
    return {"segmentation_prediction": prediction}, diagnostics
