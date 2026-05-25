from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import Settings
from src.domain.cache import request_result_dir
from src.domain.run_workspace import get_run_tmp_dir
from src.execution_profiles import PipelineExecutionConfig, resolve_backend
from src.schemas import RunRequest
from src.services.processing import run_detection


def _load_request(path: Path) -> RunRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RunRequest.model_validate(payload)


def _copy_optional(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _stage_duration_ms(timing_payload: dict[str, Any], stage_name: str) -> float | None:
    for stage in timing_payload.get("stages", []):
        if isinstance(stage, dict) and stage.get("name") == stage_name:
            duration = stage.get("duration_ms")
            if isinstance(duration, (int, float)):
                return float(duration)
    return None


def _collect_summary(metadata: dict[str, Any], timing: dict[str, Any]) -> dict[str, Any]:
    return {
        "bandon_crop_skip_enabled": metadata.get("bandon_crop_skip_enabled"),
        "crop_count_total": metadata.get("crop_count_total"),
        "forward_call_count": metadata.get("crop_count_forwarded"),
        "crop_count_forwarded": metadata.get("crop_count_forwarded"),
        "crop_count_skipped_before_forward": metadata.get("crop_count_skipped_before_forward"),
        "crop_skip_reason_counts": metadata.get("crop_skip_reason_counts"),
        "min_valid_ratio_within_aoi": metadata.get("min_valid_ratio_within_aoi"),
        "input_shape": metadata.get("input_shape"),
        "bandon_forward_duration_ms": _stage_duration_ms(timing, "inference.bandon.forward"),
        "total_inference_duration_ms": _stage_duration_ms(timing, "inference"),
    }


def _progress(prefix: str):
    def _callback(value: float, message: str) -> None:
        print(f"[{prefix}] {value:.0%} {message}")

    return _callback


def _run_variant(
    *,
    request: RunRequest,
    variant: str,
    settings: Settings,
    benchmark_id: str,
    out_dir: Path,
) -> dict[str, Any]:
    variant_out_dir = out_dir / variant
    variant_out_dir.mkdir(parents=True, exist_ok=True)

    variant_settings = settings.model_copy(
        update={
            "keep_intermediate_artifacts": True,
            "bandon_skip_invalid_crops": variant == "skip_on",
            "bandon_skip_outside_aoi_crops": True,
            "bandon_skip_nodata_crops": True,
            "bandon_min_valid_ratio_within_aoi": 0.01,
            "persistence_backend": "filesystem",
        }
    )
    backend = resolve_backend(PipelineExecutionConfig(model_backend="bandon_mps"), settings=variant_settings)
    configured_settings = backend.configure_settings(variant_settings).model_copy(
        update={"keep_intermediate_artifacts": True}
    )

    response = run_detection(
        request,
        settings=configured_settings,
        progress=_progress(variant),
        inference_runner=backend.create_inference_runner(configured_settings),
        model_backend=backend.model_backend,
        remote_patch_budget_enabled=backend.enforce_remote_patch_budget(),
        request_hash_context={
            **backend.request_hash_context(configured_settings),
            "benchmark_id": benchmark_id,
            "benchmark_variant": variant,
        },
    )
    if not response.success or response.summary is None:
        raise RuntimeError(
            f"{variant} run failed: {response.error_code or 'unknown'} {response.error_message or ''}".strip()
        )

    request_hash = response.summary.request_hash
    result_dir = request_result_dir(configured_settings, request_hash)
    tmp_dir = get_run_tmp_dir(configured_settings, request_hash)
    bandon_run_dir = tmp_dir / "bandon_run"
    timing_path = result_dir / "timing.json"
    metadata_path = bandon_run_dir / "run_metadata.json"
    if not timing_path.exists():
        raise RuntimeError(f"{variant} timing.json not found at {timing_path}")
    if not metadata_path.exists():
        raise RuntimeError(f"{variant} run_metadata.json not found at {metadata_path}")

    _copy_optional(timing_path, variant_out_dir / "timing.json")
    _copy_optional(metadata_path, variant_out_dir / "run_metadata.json")
    _copy_optional(bandon_run_dir / "change_mask.png", variant_out_dir / "change_mask.png")
    _copy_optional(bandon_run_dir / "change_probability.npy", variant_out_dir / "change_probability.npy")
    _copy_optional(result_dir / "building_change_mask.tif", variant_out_dir / "building_change_mask.tif")
    _copy_optional(result_dir / "change_probability.tif", variant_out_dir / "change_probability.tif")

    timing_payload = json.loads((variant_out_dir / "timing.json").read_text(encoding="utf-8"))
    metadata_payload = json.loads((variant_out_dir / "run_metadata.json").read_text(encoding="utf-8"))
    summary = _collect_summary(metadata_payload, timing_payload)
    summary["request_hash"] = request_hash
    return summary


def _print_summary(skip_off: dict[str, Any], skip_on: dict[str, Any]) -> None:
    before_calls = float(skip_off.get("forward_call_count") or 0)
    after_calls = float(skip_on.get("forward_call_count") or 0)
    before_ms = skip_off.get("bandon_forward_duration_ms")
    after_ms = skip_on.get("bandon_forward_duration_ms")
    call_reduction = ((before_calls - after_calls) / before_calls * 100.0) if before_calls else 0.0
    duration_reduction = (
        ((float(before_ms) - float(after_ms)) / float(before_ms) * 100.0)
        if isinstance(before_ms, (int, float)) and isinstance(after_ms, (int, float)) and float(before_ms) > 0
        else None
    )
    payload = {
        "skip_off": skip_off,
        "skip_on": skip_on,
        "comparison": {
            "forward_call_reduction_percent": round(call_reduction, 2),
            "forward_duration_reduction_percent": round(duration_reduction, 2) if duration_reduction is not None else None,
        },
    }
    print(json.dumps(payload, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a same-request BANDON crop-skip benchmark.")
    parser.add_argument("--request-json", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--benchmark-id", type=str, default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    request = _load_request(args.request_json)
    settings = Settings()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    skip_off = _run_variant(
        request=request,
        variant="skip_off",
        settings=settings,
        benchmark_id=args.benchmark_id,
        out_dir=args.out_dir,
    )
    skip_on = _run_variant(
        request=request,
        variant="skip_on",
        settings=settings,
        benchmark_id=args.benchmark_id,
        out_dir=args.out_dir,
    )

    crop_total = int(skip_on.get("crop_count_total") or 0)
    forwarded = int(skip_on.get("crop_count_forwarded") or 0)
    skipped = int(skip_on.get("crop_count_skipped_before_forward") or 0)
    if crop_total != int(skip_off.get("crop_count_total") or skip_off.get("forward_call_count") or 0):
        raise RuntimeError("skip_on crop_count_total does not match skip_off crop count.")
    if forwarded + skipped != crop_total:
        raise RuntimeError("skip_on forwarded + skipped does not equal crop_count_total.")
    if int(skip_on.get("forward_call_count") or 0) != forwarded:
        raise RuntimeError("skip_on forward_call_count does not equal crop_count_forwarded.")

    _print_summary(skip_off, skip_on)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
