#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-device MTGCDNet inference on macOS Apple Silicon."
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
        choices=["mps", "cpu"],
        default="mps",
        help="Inference device. Use cpu only if MPS is unavailable.",
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
    return parser.parse_args()


ARGS = parse_args()
if ARGS.allow_mps_fallback:
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

import mmcv  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
import torch  # noqa: E402
from mmcv.runner import load_checkpoint  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mmseg.models import build_segmentor  # noqa: E402


def resolve_device(requested: str) -> torch.device:
    if requested == "mps":
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        raise RuntimeError(
            "MPS was requested but torch.backends.mps.is_available() is False."
        )
    return torch.device("cpu")


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


def load_and_normalize_rgb(path: Path) -> np.ndarray:
    img = mmcv.imread(str(path), flag="color", backend="cv2")
    if img is None:
        raise RuntimeError(f"Failed to read image: {path}")
    img = mmcv.imnormalize(img, mean=np.array([0, 0, 0], dtype=np.float32), std=np.array([255, 255, 255], dtype=np.float32), to_rgb=True)
    return img.astype(np.float32)


def build_input_tensor(image_a: np.ndarray, image_b: np.ndarray, device: torch.device) -> torch.Tensor:
    if image_a.shape != image_b.shape:
        raise RuntimeError(
            f"Input image shapes must match. Got {image_a.shape} and {image_b.shape}."
        )
    stacked = np.concatenate([image_a, image_b], axis=2)
    stacked = np.transpose(stacked, (2, 0, 1))[None, ...]
    return torch.from_numpy(np.ascontiguousarray(stacked)).to(device)


def build_img_meta(image_shape: tuple[int, int, int], image_a: Path, image_b: Path) -> list[list[dict[str, Any]]]:
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
            "mean": np.array([0, 0, 0], dtype=np.float32),
            "std": np.array([255, 255, 255], dtype=np.float32),
            "to_rgb": True,
        },
    }
    return [[meta]]


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
    repo_root = REPO_ROOT
    config_path = (repo_root / ARGS.config).resolve() if not Path(ARGS.config).is_absolute() else Path(ARGS.config)
    checkpoint_path = Path(ARGS.checkpoint).resolve()
    image_a_path = Path(ARGS.image_a).resolve()
    image_b_path = Path(ARGS.image_b).resolve()
    outdir = Path(ARGS.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    device = resolve_device(ARGS.device)
    cfg = prepare_config(config_path)
    mps_test_cfg = apply_mps_safe_test_cfg(cfg, device)
    model = build_segmentor(cfg.model, test_cfg=cfg.get("test_cfg"))
    checkpoint = load_checkpoint(model, str(checkpoint_path), map_location="cpu")
    checkpoint_meta = checkpoint.get("meta", {}) or {}
    classes = checkpoint_meta.get("CLASSES") or ["unchange", "change"]
    palette = checkpoint_meta.get("PALETTE") or [[0, 0, 0], [255, 255, 255]]
    model.CLASSES = classes
    model.PALETTE = palette
    model.cfg = cfg
    model.to(device)
    model.eval()

    image_a = load_and_normalize_rgb(image_a_path)
    image_b = load_and_normalize_rgb(image_b_path)
    input_tensor = build_input_tensor(image_a, image_b, device)
    img_metas = build_img_meta(image_a.shape, image_a_path, image_b_path)

    with torch.no_grad():
        result = model(return_loss=False, img=[input_tensor], img_metas=img_metas, rescale=True)

    if not isinstance(result, list) or not result or not isinstance(result[0], list) or not result[0]:
        raise RuntimeError(f"Unexpected inference output structure: {type(result)}")

    prediction = result[0][0]
    if not isinstance(prediction, np.ndarray) or prediction.ndim != 3 or prediction.shape[0] != 2:
        raise RuntimeError(f"Expected a (2, H, W) probability tensor, got {type(prediction)} with shape {getattr(prediction, 'shape', None)}")

    change_probability = prediction[1].astype(np.float32)
    change_mask = np.argmax(prediction, axis=0).astype(np.uint8)

    probability_npy = outdir / "change_probability.npy"
    probability_png = outdir / "change_probability.png"
    mask_png = outdir / "change_mask.png"
    overlay_png = outdir / "change_overlay.png"
    metadata_json = outdir / "run_metadata.json"

    np.save(probability_npy, change_probability)
    save_probability_png(probability_png, change_probability)
    save_mask_png(mask_png, change_mask)
    save_overlay(overlay_png, image_b_path, change_mask)

    metadata = {
        "repo_root": str(repo_root),
        "config": str(config_path),
        "checkpoint": str(checkpoint_path),
        "image_a": str(image_a_path),
        "image_b": str(image_b_path),
        "device_requested": ARGS.device,
        "device_resolved": str(device),
        "allow_mps_fallback": ARGS.allow_mps_fallback,
        "pytorch_enable_mps_fallback": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK"),
        "torch_version": torch.__version__,
        "mmcv_version": mmcv.__version__,
        "mps_built": bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_built()),
        "mps_available": bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_available()),
        "mps_test_cfg": mps_test_cfg,
        "classes": classes,
        "palette": palette,
        "input_shape": list(image_a.shape),
        "outputs": {
            "change_probability_npy": str(probability_npy),
            "change_probability_png": str(probability_png),
            "change_mask_png": str(mask_png),
            "change_overlay_png": str(overlay_png),
        },
    }
    metadata_json.write_text(json.dumps(metadata, indent=2))

    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
