from __future__ import annotations

from typing import Any

import numpy as np
import torch


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _tensor_like_summary(value: Any, *, max_items: int = 32) -> dict[str, Any]:
    shapes: list[list[int]] = []
    dtypes: list[str] = []
    devices: list[str] = []
    count = 0
    seen_ids: set[int] = set()

    def visit(item: Any) -> None:
        nonlocal count
        if count >= max_items:
            return
        if isinstance(item, torch.Tensor):
            seen_id = id(item)
            if seen_id in seen_ids:
                return
            seen_ids.add(seen_id)
            count += 1
            _append_unique(shapes, [int(dim) for dim in item.shape])
            _append_unique(dtypes, str(item.dtype))
            _append_unique(devices, str(item.device))
            return
        if isinstance(item, np.ndarray):
            seen_id = id(item)
            if seen_id in seen_ids:
                return
            seen_ids.add(seen_id)
            count += 1
            _append_unique(shapes, [int(dim) for dim in item.shape])
            _append_unique(dtypes, str(item.dtype))
            _append_unique(devices, "cpu")
            return
        if isinstance(item, dict):
            seen_id = id(item)
            if seen_id in seen_ids:
                return
            seen_ids.add(seen_id)
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple)):
            seen_id = id(item)
            if seen_id in seen_ids:
                return
            seen_ids.add(seen_id)
            for nested in item:
                visit(nested)

    visit(value)
    return {
        "count": count,
        "shapes": shapes,
        "dtypes": dtypes,
        "devices": devices,
    }


def _summarize_model_parameters(model: torch.nn.Module, *, max_items: int = 32) -> dict[str, Any]:
    devices: list[str] = []
    dtypes: list[str] = []
    count = 0
    for parameter in model.parameters():
        count += 1
        if len(devices) < max_items:
            _append_unique(devices, str(parameter.device))
        if len(dtypes) < max_items:
            _append_unique(dtypes, str(parameter.dtype))
    return {
        "count": count,
        "devices": devices,
        "dtypes": dtypes,
    }


def _compact_pair_value(height: int | None, width: int | None) -> int | list[int] | None:
    if height is None or width is None:
        return None
    if height == width:
        return int(height)
    return [int(height), int(width)]


def build_slide_crop_bounds(
    *,
    input_height: int,
    input_width: int,
    crop_height: int | None,
    crop_width: int | None,
    stride_height: int | None,
    stride_width: int | None,
) -> list[dict[str, int]]:
    if not crop_height or not crop_width or not stride_height or not stride_width:
        return [
            {
                "index": 0,
                "x0": 0,
                "y0": 0,
                "x1": int(input_width),
                "y1": int(input_height),
                "width": int(input_width),
                "height": int(input_height),
            }
        ]
    if crop_height >= input_height and crop_width >= input_width:
        return [
            {
                "index": 0,
                "x0": 0,
                "y0": 0,
                "x1": int(input_width),
                "y1": int(input_height),
                "width": int(input_width),
                "height": int(input_height),
            }
        ]

    h_grids = max(input_height - crop_height + stride_height - 1, 0) // stride_height + 1
    w_grids = max(input_width - crop_width + stride_width - 1, 0) // stride_width + 1
    bounds: list[dict[str, int]] = []
    for h_idx in range(h_grids):
        for w_idx in range(w_grids):
            y0 = h_idx * stride_height
            x0 = w_idx * stride_width
            y1 = min(y0 + crop_height, input_height)
            x1 = min(x0 + crop_width, input_width)
            y0 = max(y1 - crop_height, 0)
            x0 = max(x1 - crop_width, 0)
            bounds.append(
                {
                    "index": len(bounds),
                    "x0": int(x0),
                    "y0": int(y0),
                    "x1": int(x1),
                    "y1": int(y1),
                    "width": int(x1 - x0),
                    "height": int(y1 - y0),
                }
            )
    return bounds


def _count_output_change_pixels(output_tensor: Any, *, valid_height: int, valid_width: int) -> int | None:
    if valid_height <= 0 or valid_width <= 0:
        return 0
    if isinstance(output_tensor, torch.Tensor):
        tensor = output_tensor.detach()
        if tensor.ndim == 4:
            predicted = tensor.argmax(dim=1)
        elif tensor.ndim == 3 and tensor.shape[0] > 1:
            predicted = tensor.argmax(dim=0)
        else:
            return None
        predicted = predicted[..., :valid_height, :valid_width]
        return int(torch.count_nonzero(predicted).item())
    if isinstance(output_tensor, np.ndarray):
        array = output_tensor
        if array.ndim == 4:
            predicted = array.argmax(axis=1)
        elif array.ndim == 3 and array.shape[0] > 1:
            predicted = array.argmax(axis=0)
        else:
            return None
        predicted = predicted[..., :valid_height, :valid_width]
        return int(np.count_nonzero(predicted))
    return None


def build_crop_summary(
    *,
    index: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    duration_ms: float,
    input_tensor: torch.Tensor,
    output_tensor: Any,
    previous_bounds: tuple[int, int, int, int] | None = None,
    coverage_before_pixels: int | None = None,
    aoi_pixels: int | None = None,
) -> dict[str, Any]:
    crop_height = int(y1 - y0)
    crop_width = int(x1 - x0)
    crop_area_pixels = int(input_tensor.shape[-2]) * int(input_tensor.shape[-1])
    valid_pixels = int(crop_height * crop_width)
    padding_pixels = max(crop_area_pixels - valid_pixels, 0)
    valid_ratio = (valid_pixels / crop_area_pixels) if crop_area_pixels else None
    padding_ratio = (padding_pixels / crop_area_pixels) if crop_area_pixels else None
    immediate_overlap_pixels = 0
    if previous_bounds is not None:
        prev_x0, prev_y0, prev_x1, prev_y1 = previous_bounds
        overlap_width = max(0, min(x1, prev_x1) - max(x0, prev_x0))
        overlap_height = max(0, min(y1, prev_y1) - max(y0, prev_y0))
        immediate_overlap_pixels = int(overlap_width * overlap_height)
    immediate_overlap_ratio = (
        immediate_overlap_pixels / crop_area_pixels if crop_area_pixels else None
    )
    coverage_redundancy_pixels = int(coverage_before_pixels or 0)
    coverage_redundancy_ratio = (
        coverage_redundancy_pixels / crop_area_pixels if crop_area_pixels else None
    )
    output_nonzero_pixels = _count_output_change_pixels(
        output_tensor,
        valid_height=valid_pixels and crop_height or 0,
        valid_width=valid_pixels and crop_width or 0,
    )
    output_nonzero_ratio = (
        output_nonzero_pixels / valid_pixels if valid_pixels and output_nonzero_pixels is not None else None
    )
    input_summary = _tensor_like_summary(input_tensor)
    output_summary = _tensor_like_summary(output_tensor)
    payload: dict[str, Any] = {
        "index": int(index),
        "x0": int(x0),
        "y0": int(y0),
        "x1": int(x1),
        "y1": int(y1),
        "width": int(crop_width),
        "height": int(crop_height),
        "crop_area_pixels": int(crop_area_pixels),
        "duration_ms": round(float(duration_ms), 2),
        "input_shape": input_summary["shapes"][0] if input_summary["shapes"] else [int(dim) for dim in input_tensor.shape],
        "input_dtype": input_summary["dtypes"][0] if input_summary["dtypes"] else str(input_tensor.dtype),
        "input_device": input_summary["devices"][0] if input_summary["devices"] else str(input_tensor.device),
        "output_shape": output_summary["shapes"][0] if output_summary["shapes"] else None,
        "output_dtype": output_summary["dtypes"][0] if output_summary["dtypes"] else None,
        "output_device": output_summary["devices"][0] if output_summary["devices"] else None,
        "padding_pixels": int(padding_pixels),
        "padding_ratio": padding_ratio,
        "valid_pixels": int(valid_pixels),
        "valid_ratio": valid_ratio,
        "aoi_pixels": int(aoi_pixels) if aoi_pixels is not None else None,
        "aoi_ratio": (aoi_pixels / crop_area_pixels) if aoi_pixels is not None and crop_area_pixels else None,
        "overlap_pixels_with_previous": int(immediate_overlap_pixels),
        "overlap_ratio_with_previous": immediate_overlap_ratio,
        "coverage_redundancy_pixels": int(coverage_redundancy_pixels),
        "coverage_redundancy_ratio": coverage_redundancy_ratio,
        "output_nonzero_pixels": int(output_nonzero_pixels) if output_nonzero_pixels is not None else None,
        "output_nonzero_ratio": output_nonzero_ratio,
        "contributes_to_final_output": bool(valid_pixels > 0),
    }
    return payload


def summarize_crop_diagnostics(
    crop_summaries: list[dict[str, Any]],
    *,
    input_height: int,
    input_width: int,
    crop_height: int | None,
    crop_width: int | None,
    stride_height: int | None,
    stride_width: int | None,
    coverage_counts: np.ndarray | None = None,
) -> dict[str, Any]:
    if not crop_summaries:
        return {}

    crop_count = len(crop_summaries)
    durations = np.array([float(crop.get("duration_ms", 0.0)) for crop in crop_summaries], dtype=np.float64)
    valid_pixels = np.array([float(crop.get("valid_pixels") or 0) for crop in crop_summaries], dtype=np.float64)
    padding_pixels = np.array([float(crop.get("padding_pixels") or 0) for crop in crop_summaries], dtype=np.float64)
    overlap_pixels = np.array([float(crop.get("coverage_redundancy_pixels") or 0) for crop in crop_summaries], dtype=np.float64)
    output_nonzero_pixels = np.array([float(crop.get("output_nonzero_pixels") or 0) for crop in crop_summaries], dtype=np.float64)
    valid_ratios = np.array([float(crop.get("valid_ratio") or 0.0) for crop in crop_summaries], dtype=np.float64)
    padding_ratios = np.array([float(crop.get("padding_ratio") or 0.0) for crop in crop_summaries], dtype=np.float64)
    output_nonzero_ratios = np.array([float(crop.get("output_nonzero_ratio") or 0.0) for crop in crop_summaries], dtype=np.float64)
    aoi_ratios = [float(crop["aoi_ratio"]) for crop in crop_summaries if crop.get("aoi_ratio") is not None]
    overlap_ratio_values = np.array([float(crop.get("coverage_redundancy_ratio") or 0.0) for crop in crop_summaries], dtype=np.float64)

    crop_area_pixels = int(crop_summaries[0].get("crop_area_pixels") or 0)
    if crop_area_pixels <= 0 and crop_height and crop_width:
        crop_area_pixels = int(crop_height * crop_width)
    if crop_area_pixels <= 0:
        crop_area_pixels = int(input_width * input_height)

    overlap_h = int(max((crop_height or 0) - (stride_height or 0), 0)) if crop_height and stride_height else None
    overlap_w = int(max((crop_width or 0) - (stride_width or 0), 0)) if crop_width and stride_width else None
    overlap_pixels_compact = _compact_pair_value(overlap_h, overlap_w)
    if overlap_h is not None and crop_height:
        overlap_ratio_h = overlap_h / crop_height
    else:
        overlap_ratio_h = None
    if overlap_w is not None and crop_width:
        overlap_ratio_w = overlap_w / crop_width
    else:
        overlap_ratio_w = None
    if overlap_ratio_h is not None and overlap_ratio_w is not None and overlap_ratio_h == overlap_ratio_w:
        overlap_ratio_compact = overlap_ratio_h
    elif overlap_ratio_h is not None or overlap_ratio_w is not None:
        overlap_ratio_compact = {
            "height": overlap_ratio_h,
            "width": overlap_ratio_w,
        }
    else:
        overlap_ratio_compact = None

    mean_crop_ms = float(np.mean(durations))
    median_crop_ms = float(np.median(durations))
    p95_crop_ms = float(np.percentile(durations, 95))
    crop_duration_cv = float(np.std(durations) / mean_crop_ms) if mean_crop_ms else 0.0
    slowest_index = int(np.argmax(durations))
    fastest_index = int(np.argmin(durations))
    slowest_crop_ms = float(durations[slowest_index])
    fastest_crop_ms = float(durations[fastest_index])
    slowest_to_median_ratio = (slowest_crop_ms / median_crop_ms) if median_crop_ms else None
    p95_to_median_ratio = (p95_crop_ms / median_crop_ms) if median_crop_ms else None
    uniform_crop_cost = bool(crop_duration_cv < 0.15 and (slowest_to_median_ratio or 0.0) < 1.5)

    low_valid_threshold = 0.01
    low_aoi_threshold = 0.01
    low_output_threshold = 0.001
    high_padding_threshold = 0.0
    high_overlap_threshold = 0.25

    empty_or_low_contribution_crop_count = 0
    high_padding_crop_count = 0
    high_overlap_crop_count = 0
    for crop in crop_summaries:
        valid_ratio = float(crop.get("valid_ratio") or 0.0)
        aoi_ratio = crop.get("aoi_ratio")
        output_nonzero_ratio = float(crop.get("output_nonzero_ratio") or 0.0)
        padding_ratio = float(crop.get("padding_ratio") or 0.0)
        overlap_ratio = float(crop.get("coverage_redundancy_ratio") or 0.0)
        if (
            valid_ratio <= low_valid_threshold
            or (aoi_ratio is not None and float(aoi_ratio) <= low_aoi_threshold)
            or output_nonzero_ratio <= low_output_threshold
        ):
            empty_or_low_contribution_crop_count += 1
        if padding_ratio > high_padding_threshold:
            high_padding_crop_count += 1
        if overlap_ratio > high_overlap_threshold:
            high_overlap_crop_count += 1

    if coverage_counts is not None:
        duplicate_coverage_pixels = int(np.count_nonzero(coverage_counts > 1))
        duplicate_coverage_ratio = duplicate_coverage_pixels / float(input_width * input_height) if input_width and input_height else None
    else:
        duplicate_coverage_pixels = int(round(float(np.sum(overlap_pixels))))
        duplicate_coverage_ratio = duplicate_coverage_pixels / float(input_width * input_height) if input_width and input_height else None

    crop_summary_payload: dict[str, Any] = {}
    if crop_count <= 100:
        crop_summary_payload["crop_summaries"] = crop_summaries
    else:
        crop_summary_payload["crop_summaries"] = None
        crop_summary_payload["top_slowest_crop_summaries"] = sorted(
            crop_summaries, key=lambda item: float(item.get("duration_ms", 0.0)), reverse=True
        )[:20]
        crop_summary_payload["top_high_padding_crop_summaries"] = sorted(
            crop_summaries, key=lambda item: float(item.get("padding_ratio") or 0.0), reverse=True
        )[:20]
        crop_summary_payload["top_low_contribution_crop_summaries"] = sorted(
            crop_summaries,
            key=lambda item: (
                float(item.get("output_nonzero_ratio") or 0.0),
                float(item.get("valid_ratio") or 0.0),
                float(item.get("padding_ratio") or 0.0),
            ),
        )[:20]

    payload: dict[str, Any] = {
        "crop_count": crop_count,
        "forward_call_count": crop_count,
        "crop_size": _compact_pair_value(crop_height, crop_width),
        "stride": _compact_pair_value(stride_height, stride_width),
        "overlap_pixels": overlap_pixels_compact,
        "overlap_ratio": overlap_ratio_compact,
        "input_width": int(input_width),
        "input_height": int(input_height),
        "padded_width": int(input_width),
        "padded_height": int(input_height),
        "padding_total_pixels": int(np.sum(padding_pixels)),
        "padding_total_ratio": (float(np.sum(padding_pixels)) / float(crop_count * crop_area_pixels)) if crop_count and crop_area_pixels else None,
        "valid_total_ratio": (float(np.sum(valid_pixels)) / float(crop_count * crop_area_pixels)) if crop_count and crop_area_pixels else None,
        "aoi_total_ratio": float(np.mean(aoi_ratios)) if aoi_ratios else None,
        "empty_or_low_contribution_crop_count": empty_or_low_contribution_crop_count,
        "high_padding_crop_count": high_padding_crop_count,
        "high_overlap_crop_count": high_overlap_crop_count,
        "slowest_crop_index": slowest_index,
        "slowest_crop_ms": slowest_crop_ms,
        "fastest_crop_index": fastest_index,
        "fastest_crop_ms": fastest_crop_ms,
        "mean_crop_ms": mean_crop_ms,
        "median_crop_ms": median_crop_ms,
        "p95_crop_ms": p95_crop_ms,
        "crop_duration_cv": crop_duration_cv,
        "uniform_crop_cost": uniform_crop_cost,
        "slowest_to_median_ratio": slowest_to_median_ratio,
        "p95_to_median_ratio": p95_to_median_ratio,
        "duplicate_coverage_pixels": duplicate_coverage_pixels,
        "duplicate_coverage_ratio": duplicate_coverage_ratio,
        "low_valid_threshold": low_valid_threshold,
        "low_aoi_threshold": low_aoi_threshold,
        "low_output_threshold": low_output_threshold,
        "high_padding_threshold": high_padding_threshold,
        "high_overlap_threshold": high_overlap_threshold,
    }
    payload.update(crop_summary_payload)
    return payload


def count_slide_crops(
    *,
    input_height: int,
    input_width: int,
    crop_height: int | None,
    crop_width: int | None,
    stride_height: int | None,
    stride_width: int | None,
) -> int:
    if not crop_height or not crop_width or not stride_height or not stride_width:
        return 1
    if crop_height >= input_height and crop_width >= input_width:
        return 1
    h_grids = max(input_height - crop_height + stride_height - 1, 0) // stride_height + 1
    w_grids = max(input_width - crop_width + stride_width - 1, 0) // stride_width + 1
    return max(1, h_grids * w_grids)


def current_torch_mode_flags() -> dict[str, bool]:
    return {
        "no_grad_active": not torch.is_grad_enabled(),
        "inference_mode_active": bool(torch.is_inference_mode_enabled()) if hasattr(torch, "is_inference_mode_enabled") else False,
    }


def _test_cfg_crop_stride(cfg: Any) -> tuple[int | None, int | None, int | None, int | None]:
    test_cfg = getattr(cfg, "test_cfg", None)
    if not isinstance(test_cfg, dict):
        return None, None, None, None
    crop_size = test_cfg.get("crop_size")
    stride = test_cfg.get("stride")
    if not isinstance(crop_size, (list, tuple)) or len(crop_size) != 2:
        crop_height = crop_width = None
    else:
        crop_height = int(crop_size[0])
        crop_width = int(crop_size[1])
    if not isinstance(stride, (list, tuple)) or len(stride) != 2:
        stride_height = stride_width = None
    else:
        stride_height = int(stride[0])
        stride_width = int(stride[1])
    return crop_height, crop_width, stride_height, stride_width


def build_model_load_diagnostics(
    *,
    model: torch.nn.Module,
    device: torch.device,
    device_configured: str,
    model_reload_count_this_job: int,
    model_reused: bool,
    no_grad_active: bool,
    inference_mode_active: bool,
    checkpoint_path: str,
    process_id: int,
    mps_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "backend": "bandon_mps",
        "device": str(device),
        "device_configured": device_configured,
        "model_reload_count_this_job": model_reload_count_this_job,
        "model_reused": model_reused,
        "model_cached_or_reused": model_reused,
        "used_no_grad": no_grad_active,
        "used_inference_mode": inference_mode_active,
        "checkpoint_path": checkpoint_path,
        "process_id": process_id,
    }
    payload.update(_summarize_model_parameters(model))
    if mps_memory:
        payload.update(mps_memory)
    return payload


def build_forward_diagnostics(
    *,
    input_tensor: torch.Tensor,
    model: torch.nn.Module,
    result: Any,
    device: torch.device,
    device_configured: str,
    aoi_height: int,
    aoi_width: int,
    no_grad_active: bool,
    inference_mode_active: bool,
    mps_synchronize_used: bool,
    cpu_to_mps_transfer_count: int,
    mps_to_cpu_transfer_count: int,
    transfer_sites: list[str] | None = None,
    model_reload_count_this_job: int,
    model_reused: bool,
    mps_available: bool,
    mps_built: bool,
    crop_summaries: list[dict[str, Any]] | None = None,
    coverage_counts: np.ndarray | None = None,
) -> dict[str, Any]:
    input_summary = _tensor_like_summary(input_tensor)
    output_summary = _tensor_like_summary(result)
    model_summary = _summarize_model_parameters(model)
    input_height = int(input_tensor.shape[-2])
    input_width = int(input_tensor.shape[-1])
    crop_height, crop_width, stride_height, stride_width = _test_cfg_crop_stride(getattr(model, "cfg", None))
    if crop_summaries:
        crop_count = len(crop_summaries)
    else:
        crop_count = count_slide_crops(
            input_height=input_height,
            input_width=input_width,
            crop_height=crop_height,
            crop_width=crop_width,
            stride_height=stride_height,
            stride_width=stride_width,
        )
    cpu_tensor_seen_inside_forward = "cpu" in input_summary["devices"] or "cpu" in model_summary["devices"]
    if crop_summaries:
        cpu_tensor_seen_inside_forward = cpu_tensor_seen_inside_forward or any(
            crop.get("input_device") == "cpu" or crop.get("output_device") == "cpu"
            for crop in crop_summaries
        )
    cpu_fallback_observed = cpu_tensor_seen_inside_forward and device.type == "mps"
    forward_resolution_larger_than_needed: bool | None
    if aoi_height > 0 and aoi_width > 0:
        forward_resolution_larger_than_needed = input_height > aoi_height or input_width > aoi_width
    else:
        forward_resolution_larger_than_needed = None
    payload: dict[str, Any] = {
        "backend": "bandon_mps",
        "device_configured": device_configured,
        "effective_forward_device": str(device),
        "mps_available": mps_available,
        "mps_built": mps_built,
        "input_tensor_shapes": input_summary["shapes"],
        "input_tensor_dtypes": input_summary["dtypes"],
        "input_tensor_devices_before_forward": input_summary["devices"],
        "model_parameter_devices": model_summary["devices"],
        "model_parameter_dtypes": model_summary["dtypes"],
        "output_tensor_shapes": output_summary["shapes"],
        "output_tensor_dtypes": output_summary["dtypes"],
        "output_tensor_devices_after_forward": output_summary["devices"],
        "crop_count": crop_count,
        "tile_count": crop_count,
        "runs_multiple_crops": crop_count > 1,
        "forward_call_count": 1,
        "dtype_used": str(input_tensor.dtype),
        "no_grad_active": no_grad_active,
        "inference_mode_active": inference_mode_active,
        "cpu_fallback_observed": cpu_fallback_observed,
        "cpu_tensor_seen_inside_forward": cpu_tensor_seen_inside_forward,
        "cpu_to_mps_transfer_count": cpu_to_mps_transfer_count,
        "mps_to_cpu_transfer_count": mps_to_cpu_transfer_count,
        "transfer_sites": transfer_sites or [],
        "model_reload_count_this_job": model_reload_count_this_job,
        "model_reused": model_reused,
        "input_width": input_width,
        "input_height": input_height,
        "model_input_width": input_width,
        "model_input_height": input_height,
        "aoi_pixel_width": aoi_width,
        "aoi_pixel_height": aoi_height,
        "crop_height": crop_height,
        "crop_width": crop_width,
        "stride_height": stride_height,
        "stride_width": stride_width,
        "forward_resolution_larger_than_needed": forward_resolution_larger_than_needed,
        "mps_synchronize_used": mps_synchronize_used,
    }
    if crop_summaries:
        payload.update(
            summarize_crop_diagnostics(
                crop_summaries,
                input_height=input_height,
                input_width=input_width,
                crop_height=crop_height,
                crop_width=crop_width,
                stride_height=stride_height,
                stride_width=stride_width,
                coverage_counts=coverage_counts,
            )
        )
    else:
        payload["forward_call_count"] = 1
    return payload
