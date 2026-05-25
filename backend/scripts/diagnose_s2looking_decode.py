#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "BANDON-mps"
VENDOR_PYTHON = VENDOR_ROOT / ".conda-macos-mps" / "bin" / "python"
RUN_DIR = BACKEND_ROOT / "runtime_cache" / "requests" / "8e4184263b31ff2638ddceaa"
T1_PATH = RUN_DIR / "t1_wayback_rgb.tif"
T2_PATH = RUN_DIR / "t2_wayback_rgb.tif"
CHECKPOINT_PATH = PROJECT_ROOT / "mtgcdnet_s2looking_outputs" / "checkpoints" / "mtgcdnet_s2looking_fp_finetuned_best.pth"
CONFIG_PATH = VENDOR_ROOT / "workdirs_bandon" / "MTGCDNet" / "config.py"
OUT_DIR = BACKEND_ROOT / "runtime_cache" / "diagnostics" / "s2looking_decode"


CHILD_CODE = r"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import mmcv
import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F
from mmcv.runner import load_checkpoint


project_root = Path(os.environ["DIAG_PROJECT_ROOT"])
backend_root = Path(os.environ["DIAG_BACKEND_ROOT"])
vendor_root = Path(os.environ["DIAG_VENDOR_ROOT"])
for item in (backend_root, vendor_root):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from mmseg.models import build_segmentor


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


def prepare_config(path: Path) -> mmcv.Config:
    cfg = mmcv.Config.fromfile(str(path))
    cfg.model = replace_syncbn_with_bn(cfg.model)
    cfg.model.pretrained = None
    backbone = cfg.model.get("backbone")
    if isinstance(backbone, dict) and "pretrained" in backbone:
        backbone["pretrained"] = None
    cfg.model.train_cfg = None
    return cfg


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_state_dict(checkpoint: Any) -> dict[str, torch.Tensor]:
    if isinstance(checkpoint, dict):
        for key in ("state_dict", "model", "model_state_dict", "net", "network", "weights"):
            value = checkpoint.get(key)
            if isinstance(value, dict) and any(torch.is_tensor(item) for item in value.values()):
                return {str(k): v for k, v in value.items() if torch.is_tensor(v)}
        if any(torch.is_tensor(item) for item in checkpoint.values()):
            return {str(k): v for k, v in checkpoint.items() if torch.is_tensor(v)}
    return {}


def strip_prefixes(state: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    prefixes = ("module.", "model.", "segmentor.")
    stripped = {}
    for key, value in state.items():
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


def compatibility(checkpoint_obj: Any, model: torch.nn.Module) -> dict[str, Any]:
    model_state = model.state_dict()
    state = strip_prefixes(checkpoint_state_dict(checkpoint_obj))
    loadable = {
        key
        for key, value in state.items()
        if key in model_state and tuple(value.shape) == tuple(model_state[key].shape)
    }
    missing = [
        key
        for key, value in model_state.items()
        if key not in state or tuple(state[key].shape) != tuple(value.shape)
    ]
    unexpected = [
        key
        for key, value in state.items()
        if key not in model_state or tuple(model_state[key].shape) != tuple(value.shape)
    ]
    return {
        "checkpoint_top_level_keys": list(checkpoint_obj.keys())[:20] if isinstance(checkpoint_obj, dict) else [],
        "checkpoint_state_dict_key_sample": list(state.keys())[:20],
        "load_strict": False,
        "model_state_key_count": len(model_state),
        "checkpoint_state_key_count": len(state),
        "loadable_keys_count": len(loadable),
        "loadable_key_ratio": len(loadable) / max(len(model_state), 1),
        "missing_keys_count": len(missing),
        "unexpected_keys_count": len(unexpected),
        "missing_keys_sample": missing[:25],
        "unexpected_keys_sample": unexpected[:25],
        "checkpoint_compatibility_warning": bool(missing or unexpected),
    }


def load_filtered_checkpoint_state(model: torch.nn.Module, checkpoint_obj: Any) -> tuple[list[str], list[str]]:
    model_state = model.state_dict()
    state = strip_prefixes(checkpoint_state_dict(checkpoint_obj))
    loadable = {
        key: value
        for key, value in state.items()
        if key in model_state and tuple(value.shape) == tuple(model_state[key].shape)
    }
    missing, unexpected = model.load_state_dict(loadable, strict=False)
    return list(missing), list(unexpected)


def crop_center(path: Path, size: int = 512) -> np.ndarray:
    image = Image.open(path).convert("RGB")
    width, height = image.size
    left = max(0, (width - size) // 2)
    top = max(0, (height - size) // 2)
    return np.asarray(image.crop((left, top, left + size, top + size)), dtype=np.uint8)


def normalize(rgb: np.ndarray, method: str) -> np.ndarray:
    arr = rgb.astype(np.float32)
    if method == "mmseg_imagenet":
        mean = np.array([123.675, 116.28, 103.53], dtype=np.float32)
        std = np.array([58.395, 57.12, 57.375], dtype=np.float32)
        return (arr - mean) / std
    return arr / 255.0


def stats(values: np.ndarray) -> dict[str, float]:
    flat = values.astype(np.float32).reshape(-1)
    return {
        "min": float(np.min(flat)),
        "max": float(np.max(flat)),
        "mean": float(np.mean(flat)),
        "std": float(np.std(flat)),
        "p50": float(np.percentile(flat, 50)),
        "p95": float(np.percentile(flat, 95)),
        "p99": float(np.percentile(flat, 99)),
        "fraction_ge_0_50": float(np.mean(flat >= 0.50)),
        "fraction_ge_0_60": float(np.mean(flat >= 0.60)),
        "mask_pixel_count_ge_0_50": int(np.count_nonzero(flat >= 0.50)),
    }


def input_tensor(t1: np.ndarray, t2: np.ndarray, order: str, normalization: str, device: torch.device) -> torch.Tensor:
    first, second = (t2, t1) if order == "t2_then_t1" else (t1, t2)
    stacked = np.concatenate([normalize(first, normalization), normalize(second, normalization)], axis=2)
    stacked = np.transpose(stacked, (2, 0, 1))[None, ...]
    return torch.from_numpy(np.ascontiguousarray(stacked)).to(device)


def forward_logits(model: torch.nn.Module, tensor: torch.Tensor) -> torch.Tensor:
    features = model.extract_feat(tensor)
    logits = model.decode_head[0].forward(features)
    if logits.shape[-2:] != tensor.shape[-2:]:
        logits = F.interpolate(logits, size=tensor.shape[-2:], mode="bilinear", align_corners=False)
    return logits


def decode_all(logits: torch.Tensor) -> dict[str, dict[str, float]]:
    if logits.shape[1] < 2:
        channel1 = torch.sigmoid(logits[:, 0:1])
        outputs = {
            "sigmoid_channel_1": channel1,
            "softmax_channel_1": channel1,
            "sigmoid_channel1_minus_channel0": channel1,
            "argmax_channel_1": (channel1 >= 0.5).float(),
        }
    else:
        softmax_channel_1 = F.softmax(logits, dim=1)[:, 1:2]
        sigmoid_channel_1 = torch.sigmoid(logits[:, 1:2])
        sigmoid_delta = torch.sigmoid(logits[:, 1:2] - logits[:, 0:1])
        argmax_channel_1 = (torch.argmax(logits, dim=1, keepdim=True) == 1).float()
        outputs = {
            "softmax_channel_1": softmax_channel_1,
            "sigmoid_channel_1": sigmoid_channel_1,
            "sigmoid_channel1_minus_channel0": sigmoid_delta,
            "argmax_channel_1": argmax_channel_1,
        }
    return {name: stats(value.detach().cpu().numpy()) for name, value in outputs.items()}


def run_device(device_name: str, t1: np.ndarray, t2: np.ndarray, cfg_path: Path, checkpoint_path: Path) -> dict[str, Any]:
    if device_name == "mps" and not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
        return {"available": False, "error": "torch.backends.mps.is_available() is false"}
    device = torch.device(device_name)
    cfg = prepare_config(cfg_path)
    model = build_segmentor(cfg.model, test_cfg=cfg.get("test_cfg"))
    checkpoint_obj = torch.load(str(checkpoint_path), map_location="cpu")
    compat = compatibility(checkpoint_obj, model)
    stripped_state = strip_prefixes(checkpoint_state_dict(checkpoint_obj))
    sample_key = next((key for key in stripped_state if key in model.state_dict()), None)
    before_sample_equal = None
    after_sample_equal = None
    after_sample_changed = None
    before_sample = None
    if sample_key is not None:
        before_sample = model.state_dict()[sample_key].detach().cpu().clone()
        before_sample_equal = bool(torch.equal(before_sample, stripped_state[sample_key]))
    # Probe the app's previous mmcv load behavior on a throwaway model. The S2Looking
    # checkpoint uses model_state_dict, which mmcv does not load into this model.
    probe_cfg = prepare_config(cfg_path)
    probe_model = build_segmentor(probe_cfg.model, test_cfg=probe_cfg.get("test_cfg"))
    probe_before = probe_model.state_dict()[sample_key].detach().cpu().clone() if sample_key is not None else None
    load_checkpoint(probe_model, str(checkpoint_path), map_location="cpu")
    if sample_key is not None and before_sample is not None:
        after_sample = probe_model.state_dict()[sample_key].detach().cpu()
        after_sample_equal = bool(torch.equal(after_sample, stripped_state[sample_key]))
        after_sample_changed = bool(not torch.equal(probe_before, after_sample)) if probe_before is not None else None
    missing_after_load, unexpected_after_load = load_filtered_checkpoint_state(model, checkpoint_obj)
    model.to(device)
    model.eval()
    cases = {}
    with torch.no_grad():
        for order in ("t1_then_t2", "t2_then_t1"):
            for normalization in ("app_0_1", "mmseg_imagenet"):
                key = f"{order}__{normalization}"
                try:
                    tensor = input_tensor(t1, t2, order, normalization, device)
                    logits = forward_logits(model, tensor)
                    cases[key] = {
                        "input_order": order,
                        "normalization": normalization,
                        "logit_min_by_channel": [float(logits[0, idx].min().detach().cpu()) for idx in range(logits.shape[1])],
                        "logit_max_by_channel": [float(logits[0, idx].max().detach().cpu()) for idx in range(logits.shape[1])],
                        "logit_mean_by_channel": [float(logits[0, idx].mean().detach().cpu()) for idx in range(logits.shape[1])],
                        "decodes": decode_all(logits),
                    }
                    if device.type == "mps":
                        torch.mps.synchronize()
                except Exception as exc:
                    cases[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "device": device_name,
        "checkpoint_compatibility": {
            **compat,
            "mmcv_load_sample_key": sample_key,
            "mmcv_load_before_equals_checkpoint_sample": before_sample_equal,
            "mmcv_load_after_equals_checkpoint_sample": after_sample_equal,
            "mmcv_load_after_changed_sample": after_sample_changed,
            "diagnostic_checkpoint_loader": "filtered_model_state_dict",
            "filtered_missing_keys_after_load_count": len(missing_after_load),
            "filtered_unexpected_keys_after_load_count": len(unexpected_after_load),
        },
        "cases": cases,
    }


def main() -> None:
    t1_path = Path(os.environ["DIAG_T1_PATH"])
    t2_path = Path(os.environ["DIAG_T2_PATH"])
    cfg_path = Path(os.environ["DIAG_CONFIG_PATH"])
    checkpoint_path = Path(os.environ["DIAG_CHECKPOINT_PATH"])
    out_json = Path(os.environ["DIAG_OUT_JSON"])
    t1 = crop_center(t1_path)
    t2 = crop_center(t2_path)
    payload = {
        "input_t1": str(t1_path),
        "input_t2": str(t2_path),
        "crop": {"strategy": "center", "width": int(t1.shape[1]), "height": int(t1.shape[0])},
        "config_path": str(cfg_path),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_sha256": sha256_file(checkpoint_path),
        "notebook_observed": {
            "normalization": "mmseg_imagenet",
            "input_order": "t1_then_t2",
            "decode_method": "sigmoid_channel1_minus_channel0",
            "threshold": 0.50,
        },
        "app_current": {
            "normalization": "app_0_1",
            "input_order": "t1_then_t2",
            "decode_method": "softmax_channel_1",
            "threshold": 0.50,
        },
        "devices": {},
    }
    for device in ("cpu", "mps"):
        payload["devices"][device] = run_device(device, t1, t2, cfg_path, checkpoint_path)
    out_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
"""


def _format_case_table(payload: dict) -> str:
    lines = [
        "| Device | Order | Normalization | Decode | Mean | P50 | P95 | P99 | frac>=0.50 | mask px |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for device_name, device_payload in payload.get("devices", {}).items():
        if not device_payload.get("available"):
            lines.append(f"| {device_name} | n/a | n/a | n/a | error: {device_payload.get('error')} | | | | | |")
            continue
        for case in device_payload.get("cases", {}).values():
            if "error" in case:
                lines.append(f"| {device_name} | error | error | error | {case['error']} | | | | | |")
                continue
            for decode_name, stats in case.get("decodes", {}).items():
                lines.append(
                    "| {device} | {order} | {norm} | {decode} | {mean:.4f} | {p50:.4f} | {p95:.4f} | {p99:.4f} | {frac:.4f} | {mask} |".format(
                        device=device_name,
                        order=case["input_order"],
                        norm=case["normalization"],
                        decode=decode_name,
                        mean=stats["mean"],
                        p50=stats["p50"],
                        p95=stats["p95"],
                        p99=stats["p99"],
                        frac=stats["fraction_ge_0_50"],
                        mask=stats["mask_pixel_count_ge_0_50"],
                    )
                )
    return "\n".join(lines)


def _write_report(payload: dict, report_path: Path) -> None:
    cpu = payload.get("devices", {}).get("cpu", {})
    compat = cpu.get("checkpoint_compatibility") or {}
    report = f"""# S2Looking Decode Diagnostic

## Inputs

- T1: `{payload["input_t1"]}`
- T2: `{payload["input_t2"]}`
- Config: `{payload["config_path"]}`
- Checkpoint: `{payload["checkpoint_path"]}`
- Checkpoint SHA256: `{payload["checkpoint_sha256"]}`
- Crop: center {payload["crop"]["width"]}x{payload["crop"]["height"]}

## Notebook Path Observed

- Normalization: `{payload["notebook_observed"]["normalization"]}`
- Input order: `{payload["notebook_observed"]["input_order"]}`
- Decode: `{payload["notebook_observed"]["decode_method"]}`
- Threshold: `{payload["notebook_observed"]["threshold"]}`

## App Current Path

- Normalization: `{payload["app_current"]["normalization"]}`
- Input order: `{payload["app_current"]["input_order"]}`
- Decode: `{payload["app_current"]["decode_method"]}`
- Threshold: `{payload["app_current"]["threshold"]}`

## Checkpoint Compatibility

- load_strict: `{compat.get("load_strict")}`
- loadable_key_ratio: `{compat.get("loadable_key_ratio")}`
- missing_keys_count: `{compat.get("missing_keys_count")}`
- unexpected_keys_count: `{compat.get("unexpected_keys_count")}`
- compatibility_warning: `{compat.get("checkpoint_compatibility_warning")}`
- mmcv_load_sample_key: `{compat.get("mmcv_load_sample_key")}`
- mmcv_load_after_equals_checkpoint_sample: `{compat.get("mmcv_load_after_equals_checkpoint_sample")}`
- mmcv_load_after_changed_sample: `{compat.get("mmcv_load_after_changed_sample")}`
- diagnostic_checkpoint_loader: `{compat.get("diagnostic_checkpoint_loader")}`
- filtered_missing_keys_after_load_count: `{compat.get("filtered_missing_keys_after_load_count")}`
- filtered_unexpected_keys_after_load_count: `{compat.get("filtered_unexpected_keys_after_load_count")}`
- missing_keys_sample: `{compat.get("missing_keys_sample")}`
- unexpected_keys_sample: `{compat.get("unexpected_keys_sample")}`

## Decode/Normalization/Input-Order Matrix

{_format_case_table(payload)}
"""
    report_path.write_text(report, encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not VENDOR_PYTHON.exists():
        raise SystemExit(f"Missing BANDON Python environment: {VENDOR_PYTHON}")
    for path in (T1_PATH, T2_PATH, CHECKPOINT_PATH, CONFIG_PATH):
        if not path.exists():
            raise SystemExit(f"Missing required input: {path}")

    child_path = OUT_DIR / "_diagnose_s2looking_child.py"
    out_json = OUT_DIR / "report.json"
    report_path = OUT_DIR / "report.md"
    child_path.write_text(textwrap.dedent(CHILD_CODE), encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "DIAG_PROJECT_ROOT": str(PROJECT_ROOT),
            "DIAG_BACKEND_ROOT": str(BACKEND_ROOT),
            "DIAG_VENDOR_ROOT": str(VENDOR_ROOT),
            "DIAG_T1_PATH": str(T1_PATH),
            "DIAG_T2_PATH": str(T2_PATH),
            "DIAG_CONFIG_PATH": str(CONFIG_PATH),
            "DIAG_CHECKPOINT_PATH": str(CHECKPOINT_PATH),
            "DIAG_OUT_JSON": str(out_json),
        }
    )
    completed = subprocess.run(
        [str(VENDOR_PYTHON), str(child_path)],
        cwd=str(VENDOR_ROOT),
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        (OUT_DIR / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
        (OUT_DIR / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
        raise SystemExit(
            "S2Looking diagnostic failed. "
            f"stdout={completed.stdout[-2000:]} stderr={completed.stderr[-4000:]}"
        )
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    _write_report(payload, report_path)
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
