#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import sys
from pathlib import Path
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-device MTGCDNet inference with auto/cpu/cuda/mps device selection."
    )
    parser.add_argument(
        "--config",
        default="workdirs_bandon/MTGCDNet/config.py",
        help="Path to the MTGCDNet config file.",
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        help="Path to the official MTGCDNet checkpoint.",
    )
    parser.add_argument("--image-a", required=True, help="First RGB image path.")
    parser.add_argument("--image-b", required=True, help="Second RGB image path.")
    parser.add_argument(
        "--device",
        choices=["auto", "cpu", "cuda", "mps"],
        default="auto",
        help="Inference device. auto prefers CUDA, then native macOS MPS, then CPU.",
    )
    parser.add_argument("--outdir", required=True, help="Output directory.")
    parser.add_argument(
        "--allow-mps-fallback",
        action="store_true",
        help=(
            "Enable PyTorch's documented CPU fallback for unsupported MPS ops "
            "(PYTORCH_ENABLE_MPS_FALLBACK=1)."
        ),
    )
    parser.add_argument("--skip-invalid-crops", action="store_true", help="Skip objectively invalid crops before model.forward.")
    parser.add_argument(
        "--skip-outside-aoi-crops",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip crops with no AOI pixels when --skip-invalid-crops is enabled.",
    )
    parser.add_argument(
        "--skip-nodata-crops",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip crops with no or near-zero valid paired imagery inside the AOI when --skip-invalid-crops is enabled.",
    )
    parser.add_argument("--t1-valid-mask", help="PNG mask for valid T1 imagery on the model input grid.")
    parser.add_argument("--t2-valid-mask", help="PNG mask for valid T2 imagery on the model input grid.")
    parser.add_argument("--aoi-mask", help="PNG mask for AOI pixels on the model input grid.")
    parser.add_argument(
        "--effective-backend",
        default="bandon_mps",
        choices=["bandon_mps", "mtgcdnet_s2looking_mps"],
        help="Configured backend name for metadata; this runner family remains bandon_mps.",
    )
    parser.add_argument(
        "--normalization",
        default="app_0_1",
        choices=["app_0_1", "mmseg_imagenet"],
        help="RGB normalization used before MTGCDNet inference.",
    )
    parser.add_argument(
        "--min-valid-ratio-within-aoi",
        type=float,
        default=0.01,
        help="Minimum valid paired imagery ratio within AOI required to forward a crop.",
    )
    return parser.parse_args()


ARGS = parse_args()
if ARGS.allow_mps_fallback:
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backend"
for path in (BACKEND_ROOT, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import mmcv  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
import torch  # noqa: E402
from mmcv.runner import load_checkpoint  # noqa: E402


from mmseg.models import build_segmentor  # noqa: E402
from src.domain.bandon_crop_skip import CropSkipDecision, should_skip_crop  # noqa: E402
from src.domain.bandon_forward_diagnostics import build_crop_summary, build_forward_diagnostics, build_model_load_diagnostics, build_slide_crop_bounds, current_torch_mode_flags  # noqa: E402
from src.domain.stage_timing import sanitize_metadata_value  # noqa: E402


def _duration_ms(start_ns: int, end_ns: int) -> float:
    return round((end_ns - start_ns) / 1_000_000, 2)


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_metadata_value(value) for key, value in metadata.items()}


def _runner_metadata(**extra: Any) -> dict[str, Any]:
    return {
        "runner_family": "bandon_mps",
        "effective_backend": ARGS.effective_backend,
        **extra,
    }


def _merge_stage_metadata(timings: ChildStageRecorder, stage_name: str, metadata: dict[str, Any]) -> None:
    for stage in reversed(timings._stages):
        if stage.get("name") == stage_name:
            existing = stage.get("metadata")
            if isinstance(existing, dict):
                stage["metadata"] = {**existing, **_safe_metadata(metadata)}
            else:
                stage["metadata"] = _safe_metadata(metadata)
            return


def _drop_legacy_backend_field_for_configured_backend(metadata: dict[str, Any]) -> dict[str, Any]:
    if ARGS.effective_backend == "mtgcdnet_s2looking_mps":
        metadata.pop("backend", None)
    return metadata


class ChildStageRecorder:
    def __init__(self) -> None:
        self._stages: list[dict[str, Any]] = []

    @contextmanager
    def stage(self, name: str, **metadata: Any):
        start_ns = time.perf_counter_ns()
        status = "success"
        error_type: str | None = None
        try:
            yield
        except Exception as exc:
            status = "failed"
            error_type = type(exc).__name__
            raise
        finally:
            payload: dict[str, Any] = {
                "name": name,
                "duration_ms": _duration_ms(start_ns, time.perf_counter_ns()),
                "status": status,
                "metadata": _safe_metadata(metadata),
            }
            if error_type is not None:
                payload["error_type"] = error_type
            self._stages.append(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": f"bandon:{Path(ARGS.outdir).resolve().name}",
            "stages": list(self._stages),
        }


def _maybe_sync_device(device: torch.device) -> None:
    if device.type == "cuda":
        try:
            torch.cuda.synchronize(device)
        except Exception:
            pass
        return
    if device.type == "mps":
        sync = getattr(getattr(torch, "mps", None), "synchronize", None)
        if callable(sync):
            sync()


def _mps_memory_metadata(device: torch.device) -> dict[str, Any]:
    if device.type != "mps":
        return {}
    mps_module = getattr(torch, "mps", None)
    current_alloc = getattr(mps_module, "current_allocated_memory", None)
    driver_alloc = getattr(mps_module, "driver_allocated_memory", None)
    payload: dict[str, Any] = {}
    if callable(current_alloc):
        try:
            payload["mps_current_allocated_memory_bytes"] = int(current_alloc())
        except Exception:
            pass
    if callable(driver_alloc):
        try:
            payload["mps_driver_allocated_memory_bytes"] = int(driver_alloc())
        except Exception:
            pass
    return payload


def _cuda_memory_metadata(device: torch.device) -> dict[str, Any]:
    if device.type != "cuda":
        return {}
    payload: dict[str, Any] = {}
    try:
        payload["cuda_current_device"] = int(torch.cuda.current_device())
    except Exception:
        pass
    try:
        payload["cuda_device_name"] = torch.cuda.get_device_name(device)
    except Exception:
        pass
    for key, getter in (
        ("cuda_memory_allocated_bytes", torch.cuda.memory_allocated),
        ("cuda_memory_reserved_bytes", torch.cuda.memory_reserved),
        ("cuda_max_memory_allocated_bytes", torch.cuda.max_memory_allocated),
        ("cuda_max_memory_reserved_bytes", torch.cuda.max_memory_reserved),
    ):
        try:
            payload[key] = int(getter(device))
        except Exception:
            pass
    return payload


def _device_memory_metadata(device: torch.device) -> dict[str, Any]:
    return {
        **_mps_memory_metadata(device),
        **_cuda_memory_metadata(device),
    }


def _mps_is_available() -> bool:
    return bool(
        sys.platform == "darwin"
        and hasattr(torch.backends, "mps")
        and torch.backends.mps.is_available()
    )


def resolve_device(requested: str) -> torch.device:
    if requested == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if _mps_is_available():
            return torch.device("mps")
        return torch.device("cpu")
    if requested == "cuda":
        if torch.cuda.is_available():
            return torch.device("cuda")
        raise RuntimeError("CUDA was requested but torch.cuda.is_available() is False.")
    if requested == "mps":
        if _mps_is_available():
            return torch.device("mps")
        raise RuntimeError(
            "MPS was requested but native macOS torch.backends.mps.is_available() is False."
        )
    if requested == "cpu":
        return torch.device("cpu")
    raise RuntimeError(f"Unsupported inference device: {requested}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_dict_from_checkpoint(checkpoint: Any) -> dict[str, torch.Tensor]:
    if isinstance(checkpoint, dict):
        for key in ("state_dict", "model", "model_state_dict", "net", "network", "weights"):
            value = checkpoint.get(key)
            if isinstance(value, dict) and any(torch.is_tensor(item) for item in value.values()):
                return {str(k): v for k, v in value.items() if torch.is_tensor(v)}
        if any(torch.is_tensor(item) for item in checkpoint.values()):
            return {str(k): v for k, v in checkpoint.items() if torch.is_tensor(v)}
    return {}


def _strip_checkpoint_prefixes(state_dict: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    prefixes = ("module.", "model.", "segmentor.")
    stripped: dict[str, torch.Tensor] = {}
    for key, value in state_dict.items():
        new_key = key
        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if new_key.startswith(prefix):
                    new_key = new_key[len(prefix):]
                    changed = True
        stripped[new_key] = value
    return stripped


def build_checkpoint_compatibility_diagnostics(
    *,
    checkpoint_path: Path,
    checkpoint_obj: Any,
    model: torch.nn.Module,
) -> dict[str, Any]:
    model_state = model.state_dict()
    raw_state = _state_dict_from_checkpoint(checkpoint_obj)
    stripped_state = _strip_checkpoint_prefixes(raw_state)
    top_level_keys = list(checkpoint_obj.keys())[:20] if isinstance(checkpoint_obj, dict) else []
    state_key_sample = list(stripped_state.keys())[:20]
    loadable_keys: list[str] = []
    missing_keys: list[str] = []
    unexpected_keys: list[str] = []
    shape_mismatch_keys: list[str] = []

    for key, value in model_state.items():
        candidate = stripped_state.get(key)
        if candidate is None:
            missing_keys.append(key)
        elif tuple(candidate.shape) == tuple(value.shape):
            loadable_keys.append(key)
        else:
            missing_keys.append(key)
            shape_mismatch_keys.append(key)

    for key, value in stripped_state.items():
        model_value = model_state.get(key)
        if model_value is None or tuple(model_value.shape) != tuple(value.shape):
            unexpected_keys.append(key)

    loaded_ratio = (len(loadable_keys) / max(len(model_state), 1))
    warning = bool(missing_keys or unexpected_keys)
    return {
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_sha256": _sha256_file(checkpoint_path),
        "checkpoint_top_level_keys": top_level_keys,
        "checkpoint_state_dict_key_sample": state_key_sample,
        "load_strict": False,
        "model_state_key_count": int(len(model_state)),
        "checkpoint_state_key_count": int(len(stripped_state)),
        "loadable_keys_count": int(len(loadable_keys)),
        "loadable_key_ratio": float(loaded_ratio),
        "missing_keys_count": int(len(missing_keys)),
        "unexpected_keys_count": int(len(unexpected_keys)),
        "shape_mismatch_keys_count": int(len(shape_mismatch_keys)),
        "missing_keys_sample": missing_keys[:25],
        "unexpected_keys_sample": unexpected_keys[:25],
        "shape_mismatch_keys_sample": shape_mismatch_keys[:25],
        "checkpoint_compatibility_warning": warning,
    }


def load_filtered_checkpoint_state(
    *,
    model: torch.nn.Module,
    checkpoint_obj: Any,
) -> tuple[list[str], list[str]]:
    model_state = model.state_dict()
    stripped_state = _strip_checkpoint_prefixes(_state_dict_from_checkpoint(checkpoint_obj))
    loadable = {
        key: value
        for key, value in stripped_state.items()
        if key in model_state and tuple(value.shape) == tuple(model_state[key].shape)
    }
    missing, unexpected = model.load_state_dict(loadable, strict=False)
    return list(missing), list(unexpected)


def replace_syncbn_with_bn(value: Any) -> Any:
    if isinstance(value, dict):
        patched = {}
        for key, item in value.items():
            if key == "norm_cfg" and isinstance(item, dict) and item.get("type") == "SyncBN":
                patched[key] = {**item, "type": "BN"}
            else:
                patched[key] = replace_syncbn_with_bn(item)
        return patched
    if isinstance(value, list):
        return [replace_syncbn_with_bn(item) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_syncbn_with_bn(item) for item in value)
    return value


def prepare_config(config_path: Path) -> mmcv.Config:
    cfg = mmcv.Config.fromfile(str(config_path))
    cfg.model = replace_syncbn_with_bn(cfg.model)
    cfg.model.pretrained = None
    backbone = cfg.model.get("backbone")
    if isinstance(backbone, dict) and "pretrained" in backbone:
        backbone["pretrained"] = None
    cfg.model.train_cfg = None
    return cfg


def _ensure_tuple2(value: Any, name: str) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    raise RuntimeError(f"Expected {name} to be a pair, got: {value!r}")


def apply_mps_safe_test_cfg(cfg: mmcv.Config, device: torch.device) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "applied": False,
        "reason": None,
        "original_crop_size": None,
        "original_stride": None,
        "patched_crop_size": None,
        "patched_stride": None,
    }
    if device.type != "mps":
        diagnostics["reason"] = "not_mps"
        return diagnostics

    test_cfg = cfg.get("test_cfg")
    if not isinstance(test_cfg, dict) or test_cfg.get("mode") != "slide":
        diagnostics["reason"] = "non_slide_test_cfg"
        return diagnostics

    crop_h, crop_w = _ensure_tuple2(test_cfg.get("crop_size"), "test_cfg.crop_size")
    stride_h, stride_w = _ensure_tuple2(test_cfg.get("stride"), "test_cfg.stride")
    diagnostics["original_crop_size"] = [crop_h, crop_w]
    diagnostics["original_stride"] = [stride_h, stride_w]

    backbone = cfg.model.get("backbone", {})
    ppm_bins = backbone.get("ppm_bins", (1, 2, 3, 6))
    max_bin = max(int(bin_size) for bin_size in ppm_bins)
    # ChangePSPNetMTL is configured with output stride 8 in MTGCDNet.
    output_stride = 8
    safe_quantum = output_stride * max_bin

    safe_crop_h = max(safe_quantum, (crop_h // safe_quantum) * safe_quantum)
    safe_crop_w = max(safe_quantum, (crop_w // safe_quantum) * safe_quantum)
    safe_stride_h = max(
        output_stride,
        int((stride_h * safe_crop_h / crop_h) // output_stride) * output_stride,
    )
    safe_stride_w = max(
        output_stride,
        int((stride_w * safe_crop_w / crop_w) // output_stride) * output_stride,
    )

    if (safe_crop_h, safe_crop_w) == (crop_h, crop_w) and (safe_stride_h, safe_stride_w) == (stride_h, stride_w):
        diagnostics["reason"] = "already_safe"
        diagnostics["patched_crop_size"] = [crop_h, crop_w]
        diagnostics["patched_stride"] = [stride_h, stride_w]
        return diagnostics

    cfg.test_cfg.crop_size = (safe_crop_h, safe_crop_w)
    cfg.test_cfg.stride = (safe_stride_h, safe_stride_w)
    diagnostics["applied"] = True
    diagnostics["reason"] = "mps_adaptive_pool_divisibility"
    diagnostics["patched_crop_size"] = [safe_crop_h, safe_crop_w]
    diagnostics["patched_stride"] = [safe_stride_h, safe_stride_w]
    return diagnostics


def apply_bandon_ablation_stride_override(cfg: mmcv.Config) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "enabled": False,
        "env_var": "BANDON_ABLATION_STRIDE",
        "requested_stride": None,
        "applied_stride": None,
        "reason": "not_configured",
    }
    raw_value = os.environ.get("BANDON_ABLATION_STRIDE")
    if raw_value is None or not raw_value.strip():
        return diagnostics

    test_cfg = cfg.get("test_cfg")
    if not isinstance(test_cfg, dict) or test_cfg.get("mode") != "slide":
        diagnostics["reason"] = "non_slide_test_cfg"
        return diagnostics

    try:
        stride = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("BANDON_ABLATION_STRIDE must be a positive integer.") from exc
    if stride <= 0:
        raise RuntimeError("BANDON_ABLATION_STRIDE must be a positive integer.")

    crop_h, crop_w = _ensure_tuple2(test_cfg.get("crop_size"), "test_cfg.crop_size")
    if stride > max(crop_h, crop_w):
        raise RuntimeError("BANDON_ABLATION_STRIDE must not exceed the active crop size.")

    cfg.test_cfg.stride = (stride, stride)
    diagnostics.update(
        {
            "enabled": True,
            "requested_stride": stride,
            "applied_stride": [stride, stride],
            "reason": "env_override",
        }
    )
    return diagnostics


def normalization_config(name: str) -> dict[str, Any]:
    if name == "mmseg_imagenet":
        return {
            "type": "mmcv.Normalize",
            "name": "mmseg_imagenet",
            "mean": [123.675, 116.28, 103.53],
            "std": [58.395, 57.12, 57.375],
            "to_rgb": True,
        }
    return {
        "type": "mmcv.Normalize",
        "name": "app_0_1",
        "mean": [0.0, 0.0, 0.0],
        "std": [255.0, 255.0, 255.0],
        "to_rgb": True,
    }


def load_and_normalize_rgb(path: Path, *, normalization: str) -> np.ndarray:
    img = mmcv.imread(str(path), flag="color", backend="cv2")
    if img is None:
        raise RuntimeError(f"Failed to read image: {path}")
    norm_cfg = normalization_config(normalization)
    img = mmcv.imnormalize(
        img,
        mean=np.array(norm_cfg["mean"], dtype=np.float32),
        std=np.array(norm_cfg["std"], dtype=np.float32),
        to_rgb=bool(norm_cfg["to_rgb"]),
    )
    return img.astype(np.float32)


def load_bool_mask(path: Path, *, expected_shape: tuple[int, int], label: str) -> np.ndarray:
    mask = np.asarray(Image.open(path).convert("L")) > 0
    if mask.shape != expected_shape:
        raise RuntimeError(f"{label} shape {mask.shape} does not match model input shape {expected_shape}.")
    return mask


def load_crop_skip_masks(expected_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if not ARGS.skip_invalid_crops:
        return None
    missing = [
        label
        for label, value in (
            ("--t1-valid-mask", ARGS.t1_valid_mask),
            ("--t2-valid-mask", ARGS.t2_valid_mask),
            ("--aoi-mask", ARGS.aoi_mask),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"--skip-invalid-crops requires {', '.join(missing)}.")
    t1_valid_mask = load_bool_mask(Path(ARGS.t1_valid_mask), expected_shape=expected_shape, label="T1 valid mask")
    t2_valid_mask = load_bool_mask(Path(ARGS.t2_valid_mask), expected_shape=expected_shape, label="T2 valid mask")
    aoi_mask = load_bool_mask(Path(ARGS.aoi_mask), expected_shape=expected_shape, label="AOI mask")
    return t1_valid_mask, t2_valid_mask, aoi_mask


def zero_change_output_like(input_tensor: torch.Tensor) -> torch.Tensor:
    batch_size = int(input_tensor.shape[0])
    height = int(input_tensor.shape[-2])
    width = int(input_tensor.shape[-1])
    output = torch.zeros((batch_size, 2, height, width), dtype=input_tensor.dtype, device=input_tensor.device)
    output[:, 0, :, :] = 1.0
    return output


def build_input_tensor(image_a: np.ndarray, image_b: np.ndarray, device: torch.device) -> torch.Tensor:
    if image_a.shape != image_b.shape:
        raise RuntimeError(
            f"Input image shapes must match. Got {image_a.shape} and {image_b.shape}."
        )
    stacked = np.concatenate([image_a, image_b], axis=2)
    stacked = np.transpose(stacked, (2, 0, 1))[None, ...]
    return torch.from_numpy(np.ascontiguousarray(stacked)).to(device)


def build_img_meta(
    image_shape: tuple[int, int, int],
    image_a: Path,
    image_b: Path,
    *,
    normalization: str,
) -> list[list[dict[str, Any]]]:
    height, width, _channels = image_shape
    meta = {
        "filename": [str(image_a), str(image_b)],
        "ori_filename": [str(image_a.name), str(image_b.name)],
        "ori_shape": (height, width, 3),
        "img_shape": (height, width, 3),
        "pad_shape": (height, width, 3),
        "scale_factor": 1.0,
        "flip": False,
        "flip_direction": None,
        "img_norm_cfg": {
            "mean": np.array(normalization_config(normalization)["mean"], dtype=np.float32),
            "std": np.array(normalization_config(normalization)["std"], dtype=np.float32),
            "to_rgb": bool(normalization_config(normalization)["to_rgb"]),
        },
    }
    return [[meta]]


def array_stats(array: np.ndarray, *, mask: np.ndarray | None = None) -> dict[str, float | None]:
    values = array[mask] if mask is not None else array.reshape(-1)
    values = values.astype(np.float32, copy=False)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "p01": None,
            "p05": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "fraction_ge_0_50": None,
            "fraction_ge_0_60": None,
            "fraction_ge_0_75": None,
            "fraction_ge_0_90": None,
        }
    return {
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "p01": float(np.percentile(values, 1)),
        "p05": float(np.percentile(values, 5)),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
        "p99": float(np.percentile(values, 99)),
        "fraction_ge_0_50": float(np.mean(values >= 0.50)),
        "fraction_ge_0_60": float(np.mean(values >= 0.60)),
        "fraction_ge_0_75": float(np.mean(values >= 0.75)),
        "fraction_ge_0_90": float(np.mean(values >= 0.90)),
    }


def channel_stats(array: np.ndarray) -> dict[str, list[float]]:
    return {
        "output_min_by_channel": [float(np.min(array[index])) for index in range(array.shape[0])],
        "output_max_by_channel": [float(np.max(array[index])) for index in range(array.shape[0])],
        "output_mean_by_channel": [float(np.mean(array[index])) for index in range(array.shape[0])],
        "output_std_by_channel": [float(np.std(array[index])) for index in range(array.shape[0])],
    }


def save_probability_png(path: Path, probability: np.ndarray) -> None:
    probability = np.clip(probability, 0.0, 1.0)
    img = Image.fromarray((probability * 65535.0).astype(np.uint16))
    img.save(path)


def save_mask_png(path: Path, mask: np.ndarray) -> None:
    img = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    img.save(path)


def save_overlay(path: Path, image_b_path: Path, mask: np.ndarray) -> None:
    base = Image.open(image_b_path).convert("RGBA")
    mask_rgba = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
    mask_rgba[mask > 0] = [255, 0, 0, 120]
    overlay = Image.alpha_composite(base, Image.fromarray(mask_rgba, mode="RGBA"))
    overlay.save(path)


def main() -> int:
    timings = ChildStageRecorder()
    repo_root = REPO_ROOT
    with timings.stage("runner_startup", **_runner_metadata(requested_device=ARGS.device)):
        config_path = (repo_root / ARGS.config).resolve() if not Path(ARGS.config).is_absolute() else Path(ARGS.config)
        checkpoint_path = Path(ARGS.checkpoint).resolve()
        image_a_path = Path(ARGS.image_a).resolve()
        image_b_path = Path(ARGS.image_b).resolve()
        outdir = Path(ARGS.outdir).resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        device = resolve_device(ARGS.device)

    with timings.stage(
        "model_load_or_reuse",
        **_runner_metadata(
        device=str(device),
        model_cached_or_reused=False,
        used_inference_mode=False,
        used_no_grad=False,
        model_reload_count_this_job=1,
        model_reused=False,
        process_id=os.getpid(),
        checkpoint_path=str(checkpoint_path),
        **_device_memory_metadata(device),
        ),
    ):
        cfg = prepare_config(config_path)
        mps_test_cfg = apply_mps_safe_test_cfg(cfg, device)
        mps_test_cfg["ablation_stride_override"] = apply_bandon_ablation_stride_override(cfg)
        model = build_segmentor(cfg.model, test_cfg=cfg.get("test_cfg"))
        checkpoint_obj = torch.load(str(checkpoint_path), map_location="cpu")
        checkpoint_compatibility = build_checkpoint_compatibility_diagnostics(
            checkpoint_path=checkpoint_path,
            checkpoint_obj=checkpoint_obj,
            model=model,
        )
        if ARGS.effective_backend == "mtgcdnet_s2looking_mps" and checkpoint_compatibility["loadable_key_ratio"] < 0.90:
            raise RuntimeError(
                "S2Looking checkpoint is incompatible with the configured MTGCDNet architecture: "
                f"loadable_key_ratio={checkpoint_compatibility['loadable_key_ratio']:.3f}, "
                f"missing_keys_count={checkpoint_compatibility['missing_keys_count']}, "
                f"unexpected_keys_count={checkpoint_compatibility['unexpected_keys_count']}."
            )
        if ARGS.effective_backend == "mtgcdnet_s2looking_mps":
            missing_after_load, unexpected_after_load = load_filtered_checkpoint_state(
                model=model,
                checkpoint_obj=checkpoint_obj,
            )
            checkpoint = checkpoint_obj if isinstance(checkpoint_obj, dict) else {}
            checkpoint_loader = "filtered_model_state_dict"
        else:
            checkpoint = load_checkpoint(model, str(checkpoint_path), map_location="cpu")
            missing_after_load = []
            unexpected_after_load = []
            checkpoint_loader = "mmcv_load_checkpoint"
        checkpoint_meta = checkpoint.get("meta", {}) or {}
        classes = checkpoint_meta.get("CLASSES") or ["unchange", "change"]
        palette = checkpoint_meta.get("PALETTE") or [[0, 0, 0], [255, 255, 255]]
        model.CLASSES = classes
        model.PALETTE = palette
        model.cfg = cfg
        model.to(device)
        model.eval()
        model_load_metadata = build_model_load_diagnostics(
            model=model,
            device=device,
            device_configured=ARGS.device,
            model_reload_count_this_job=1,
            model_reused=False,
            no_grad_active=False,
            inference_mode_active=False,
            checkpoint_path=str(checkpoint_path),
            process_id=os.getpid(),
            mps_memory=_device_memory_metadata(device),
        )
        model_load_metadata.update(
            {
                "device_requested": ARGS.device,
                "device_resolved": str(device),
                "cuda_available": bool(torch.cuda.is_available()),
                "cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
                "torch_cuda_version": torch.version.cuda,
                **_cuda_memory_metadata(device),
            }
        )
        model_load_metadata = _drop_legacy_backend_field_for_configured_backend(model_load_metadata)
        model_load_metadata.update(checkpoint_compatibility)
        model_load_metadata.update(
            {
                "checkpoint_loader": checkpoint_loader,
                "missing_keys_after_load_count": len(missing_after_load),
                "unexpected_keys_after_load_count": len(unexpected_after_load),
                "missing_keys_after_load_sample": missing_after_load[:25],
                "unexpected_keys_after_load_sample": unexpected_after_load[:25],
            }
        )
    _merge_stage_metadata(timings, "model_load_or_reuse", model_load_metadata)

    with timings.stage(
        "preprocess",
        **_runner_metadata(
            device=str(device),
            normalization_used=normalization_config(ARGS.normalization),
            input_order="t1_then_t2",
        ),
    ):
        image_a = load_and_normalize_rgb(image_a_path, normalization=ARGS.normalization)
        image_b = load_and_normalize_rgb(image_b_path, normalization=ARGS.normalization)
        input_tensor = build_input_tensor(image_a, image_b, device)
        img_metas = build_img_meta(image_a.shape, image_a_path, image_b_path, normalization=ARGS.normalization)
        crop_skip_masks = load_crop_skip_masks((int(image_a.shape[0]), int(image_a.shape[1])))
        if crop_skip_masks is not None:
            t1_valid_mask_for_skip, t2_valid_mask_for_skip, aoi_mask_for_skip = crop_skip_masks
            valid_pair_mask_for_skip = t1_valid_mask_for_skip & t2_valid_mask_for_skip
        else:
            aoi_mask_for_skip = None
            valid_pair_mask_for_skip = None

    mps_sync_used = False
    cuda_sync_used = False
    if device.type == "mps":
        sync = getattr(getattr(torch, "mps", None), "synchronize", None)
        mps_sync_used = callable(sync)
    elif device.type == "cuda":
        cuda_sync_used = True

    with timings.stage(
        "forward",
        **_runner_metadata(
        device=str(device),
        used_inference_mode=False,
        used_no_grad=True,
        **_device_memory_metadata(device),
        ),
    ):
        test_cfg = cfg.get("test_cfg") if hasattr(cfg, "get") else getattr(cfg, "test_cfg", None)
        if isinstance(test_cfg, dict):
            crop_height, crop_width = _ensure_tuple2(test_cfg.get("crop_size"), "test_cfg.crop_size")
            stride_height, stride_width = _ensure_tuple2(test_cfg.get("stride"), "test_cfg.stride")
        else:
            crop_height = crop_width = stride_height = stride_width = None
        crop_bounds = build_slide_crop_bounds(
            input_height=int(image_a.shape[0]),
            input_width=int(image_a.shape[1]),
            crop_height=crop_height,
            crop_width=crop_width,
            stride_height=stride_height,
            stride_width=stride_width,
        )
        crop_summaries: list[dict[str, Any]] = []
        coverage_counts = np.zeros((int(image_a.shape[0]), int(image_a.shape[1])), dtype=np.uint16)
        orig_encode_decode = model.encode_decode
        crop_bounds_iter = iter(crop_bounds)
        previous_bounds: tuple[int, int, int, int] | None = None
        crop_count_total = len(crop_bounds)
        crop_count_forwarded = 0
        crop_count_skipped_before_forward = 0
        crop_skip_reason_counts: dict[str, int] = {}

        def traced_encode_decode(img, img_metas):
            nonlocal crop_count_forwarded, crop_count_skipped_before_forward, previous_bounds
            crop_meta = next(crop_bounds_iter, None)
            if crop_meta is None:
                crop_count_forwarded += 1
                return orig_encode_decode(img, img_metas)
            x0 = int(crop_meta["x0"])
            y0 = int(crop_meta["y0"])
            x1 = int(crop_meta["x1"])
            y1 = int(crop_meta["y1"])
            decision: CropSkipDecision | None = None
            if ARGS.skip_invalid_crops and aoi_mask_for_skip is not None and valid_pair_mask_for_skip is not None:
                decision = should_skip_crop(
                    aoi_mask_for_skip[y0:y1, x0:x1],
                    valid_pair_mask_for_skip[y0:y1, x0:x1],
                    ARGS.min_valid_ratio_within_aoi,
                    skip_outside_aoi=ARGS.skip_outside_aoi_crops,
                    skip_nodata=ARGS.skip_nodata_crops,
                )
            if device.type in {"cuda", "mps"}:
                _maybe_sync_device(device)
            start_ns = time.perf_counter_ns()
            if decision is not None and decision.skip:
                output = zero_change_output_like(img)
                crop_count_skipped_before_forward += 1
                reason = decision.reason or "unknown"
                crop_skip_reason_counts[reason] = crop_skip_reason_counts.get(reason, 0) + 1
            else:
                output = orig_encode_decode(img, img_metas)
                crop_count_forwarded += 1
            if device.type in {"cuda", "mps"}:
                _maybe_sync_device(device)
            duration_ms = _duration_ms(start_ns, time.perf_counter_ns())
            coverage_before_pixels = int(np.count_nonzero(coverage_counts[y0:y1, x0:x1]))
            crop_summary = build_crop_summary(
                index=len(crop_summaries),
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                duration_ms=duration_ms,
                input_tensor=img,
                output_tensor=output,
                previous_bounds=previous_bounds,
                coverage_before_pixels=coverage_before_pixels,
            )
            if decision is not None:
                crop_summary.update(
                    {
                        "skipped_before_forward": bool(decision.skip),
                        "skip_reason": decision.reason,
                        "aoi_pixels": decision.aoi_pixels,
                        "aoi_ratio": decision.aoi_ratio,
                        "valid_inside_aoi_pixels": decision.valid_inside_aoi_pixels,
                        "valid_inside_aoi_ratio": decision.valid_inside_aoi_ratio,
                        "valid_ratio_within_aoi": decision.valid_ratio_within_aoi,
                    }
                )
            else:
                crop_summary["skipped_before_forward"] = False
            crop_summaries.append(crop_summary)
            coverage_counts[y0:y1, x0:x1] += 1
            previous_bounds = (x0, y0, x1, y1)
            return output

        model.encode_decode = traced_encode_decode
        try:
            _maybe_sync_device(device)
            with torch.no_grad():
                forward_mode_flags = current_torch_mode_flags()
                result = model(return_loss=False, img=[input_tensor], img_metas=img_metas, rescale=True)
            _maybe_sync_device(device)
            forward_metadata = build_forward_diagnostics(
                input_tensor=input_tensor,
                model=model,
                result=result,
                device=device,
                device_configured=ARGS.device,
                aoi_height=int(image_a.shape[0]),
                aoi_width=int(image_a.shape[1]),
                no_grad_active=forward_mode_flags["no_grad_active"],
                inference_mode_active=forward_mode_flags["inference_mode_active"],
                mps_synchronize_used=mps_sync_used,
                cpu_to_mps_transfer_count=2 if device.type == "mps" else 0,
                mps_to_cpu_transfer_count=0,
                transfer_sites=(
                    ["build_input_tensor.to(device)", "model.to(device)"] if device.type == "mps" else []
                ),
                model_reload_count_this_job=1,
                model_reused=False,
                mps_available=_mps_is_available(),
                mps_built=bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_built()),
                crop_summaries=crop_summaries,
                coverage_counts=coverage_counts,
            )
            forward_metadata = _drop_legacy_backend_field_for_configured_backend(forward_metadata)
            forward_metadata.update(
                {
                    "bandon_crop_skip_enabled": bool(ARGS.skip_invalid_crops and crop_skip_masks is not None),
                    "crop_count_total": int(crop_count_total),
                    "crop_count_forwarded": int(crop_count_forwarded),
                    "crop_count_skipped_before_forward": int(crop_count_skipped_before_forward),
                    "crop_skip_reason_counts": dict(crop_skip_reason_counts),
                    "min_valid_ratio_within_aoi": float(ARGS.min_valid_ratio_within_aoi),
                    "forward_call_count": int(crop_count_forwarded),
                    "device_requested": ARGS.device,
                    "device_resolved": str(device),
                    "cuda_available": bool(torch.cuda.is_available()),
                    "cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
                    "cuda_synchronize_used": bool(cuda_sync_used),
                    "torch_cuda_version": torch.version.cuda,
                    **_cuda_memory_metadata(device),
                }
            )
        finally:
            model.encode_decode = orig_encode_decode
    _merge_stage_metadata(timings, "forward", forward_metadata)

    with timings.stage("output_decode", **_runner_metadata(device=str(device), decode_method="simple_test_softmax_channel_1")):
        if not isinstance(result, list) or not result or not isinstance(result[0], list) or not result[0]:
            raise RuntimeError(f"Unexpected inference output structure: {type(result)}")

        prediction = result[0][0]
        if not isinstance(prediction, np.ndarray) or prediction.ndim != 3 or prediction.shape[0] != 2:
            raise RuntimeError(
                f"Expected a (2, H, W) probability tensor, got {type(prediction)} with shape {getattr(prediction, 'shape', None)}"
            )

        change_probability = prediction[1].astype(np.float32)
        change_mask = np.argmax(prediction, axis=0).astype(np.uint8)
        probability_stats = array_stats(change_probability)
        probability_stats_inside_aoi = array_stats(change_probability, mask=aoi_mask_for_skip) if aoi_mask_for_skip is not None else None
        output_channel_stats = channel_stats(prediction.astype(np.float32))
    _merge_stage_metadata(
        timings,
        "output_decode",
        {
            "probability_stats": probability_stats,
            "probability_stats_inside_aoi": probability_stats_inside_aoi,
            **output_channel_stats,
        },
    )

    probability_npy = outdir / "change_probability.npy"
    probability_png = outdir / "change_probability.png"
    mask_png = outdir / "change_mask.png"
    overlay_png = outdir / "change_overlay.png"
    metadata_json = outdir / "run_metadata.json"

    with timings.stage("mask_or_raster_write", **_runner_metadata(device=str(device))):
        np.save(probability_npy, change_probability)
        save_probability_png(probability_png, change_probability)
        save_mask_png(mask_png, change_mask)
        save_overlay(overlay_png, image_b_path, change_mask)

    metadata = {
        "repo_root": str(repo_root),
        "config": str(config_path),
        "checkpoint": str(checkpoint_path),
        "checkpoint_sha256": checkpoint_compatibility["checkpoint_sha256"],
        "runner_family": "bandon_mps",
        "effective_backend": ARGS.effective_backend,
        "image_a": str(image_a_path),
        "image_b": str(image_b_path),
        "device_requested": ARGS.device,
        "device_resolved": str(device),
        "allow_mps_fallback": ARGS.allow_mps_fallback,
        "pytorch_enable_mps_fallback": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK"),
        "torch_version": torch.__version__,
        "mmcv_version": mmcv.__version__,
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
        "cuda_device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "torch_cuda_version": torch.version.cuda,
        "mps_built": bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_built()),
        "mps_available": _mps_is_available(),
        "mps_test_cfg": mps_test_cfg,
        "normalization_used": normalization_config(ARGS.normalization),
        "input_order": "t1_then_t2",
        "decode_method": "simple_test_softmax_channel_1",
        "checkpoint_diagnostics": checkpoint_compatibility,
        "checkpoint_loader": checkpoint_loader,
        "missing_keys_after_load_count": len(missing_after_load),
        "unexpected_keys_after_load_count": len(unexpected_after_load),
        "missing_keys_after_load_sample": missing_after_load[:25],
        "unexpected_keys_after_load_sample": unexpected_after_load[:25],
        "probability_stats": probability_stats,
        "probability_stats_inside_aoi": probability_stats_inside_aoi,
        **output_channel_stats,
        "classes": classes,
        "palette": palette,
        "input_shape": list(image_a.shape),
        "bandon_crop_skip_enabled": bool(forward_metadata.get("bandon_crop_skip_enabled")),
        "crop_count_total": int(forward_metadata.get("crop_count_total") or forward_metadata.get("crop_count") or 0),
        "crop_count_forwarded": int(forward_metadata.get("crop_count_forwarded") or forward_metadata.get("forward_call_count") or 0),
        "crop_count_skipped_before_forward": int(forward_metadata.get("crop_count_skipped_before_forward") or 0),
        "crop_skip_reason_counts": forward_metadata.get("crop_skip_reason_counts") or {},
        "min_valid_ratio_within_aoi": float(ARGS.min_valid_ratio_within_aoi),
        **_cuda_memory_metadata(device),
        "outputs": {
            "change_probability_npy": str(probability_npy),
            "change_probability_png": str(probability_png),
            "change_mask_png": str(mask_png),
            "change_overlay_png": str(overlay_png),
        },
    }
    with timings.stage("cleanup", **_runner_metadata(device=str(device))):
        metadata["stage_timings"] = timings.to_dict()
        metadata_json.write_text(json.dumps(metadata, indent=2))

    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
