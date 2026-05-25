from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}.")
    return payload


def _find_stage_duration(payload: dict[str, Any], stage_name: str) -> float | None:
    if isinstance(payload.get("stages"), list):
        stages = payload.get("stages")
    else:
        stage_timings = payload.get("stage_timings")
        if not isinstance(stage_timings, dict):
            return None
        stages = stage_timings.get("stages")
    if not isinstance(stages, list):
        return None
    for stage in stages:
        if isinstance(stage, dict) and stage.get("name") == stage_name:
            duration_ms = stage.get("duration_ms")
            if isinstance(duration_ms, (int, float)):
                return float(duration_ms)
    return None


def _find_first_stage_duration(payload: dict[str, Any], *stage_names: str) -> float | None:
    for stage_name in stage_names:
        duration = _find_stage_duration(payload, stage_name)
        if duration is not None:
            return duration
    return None


def _format_percent(before: float, after: float) -> str:
    if before == 0:
        return "n/a"
    reduction = ((before - after) / before) * 100.0
    return f"{reduction:.2f}%"


def _extract_shape(metadata: dict[str, Any]) -> list[int] | None:
    shape = metadata.get("input_shape")
    if isinstance(shape, list) and all(isinstance(item, int) for item in shape):
        return shape
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare BANDON crop-skip off/on run metadata and timings.")
    parser.add_argument("--skip-off-timing", type=Path, required=True)
    parser.add_argument("--skip-on-timing", type=Path, required=True)
    parser.add_argument("--skip-off-metadata", type=Path, required=True)
    parser.add_argument("--skip-on-metadata", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    off_timing = _load_json(args.skip_off_timing)
    on_timing = _load_json(args.skip_on_timing)
    off_metadata = _load_json(args.skip_off_metadata)
    on_metadata = _load_json(args.skip_on_metadata)

    off_crop_count = int(off_metadata.get("crop_count_total") or off_metadata.get("crop_count") or 0)
    off_forward_count = int(off_metadata.get("crop_count_forwarded") or off_metadata.get("forward_call_count") or 0)
    on_crop_count_total = int(on_metadata.get("crop_count_total") or on_metadata.get("crop_count") or 0)
    on_forward_count = int(on_metadata.get("crop_count_forwarded") or on_metadata.get("forward_call_count") or 0)
    on_skipped = int(on_metadata.get("crop_count_skipped_before_forward") or 0)

    report = {
        "skip_off": {
            "bandon_crop_skip_enabled": bool(off_metadata.get("bandon_crop_skip_enabled")),
            "crop_count_total": off_crop_count,
            "forward_call_count": off_forward_count,
            "crop_count_forwarded": off_forward_count,
            "crop_count_skipped_before_forward": int(off_metadata.get("crop_count_skipped_before_forward") or 0),
            "crop_skip_reason_counts": off_metadata.get("crop_skip_reason_counts") or {},
            "min_valid_ratio_within_aoi": off_metadata.get("min_valid_ratio_within_aoi"),
            "forward_duration_ms": _find_first_stage_duration(off_timing, "inference.bandon.forward", "forward"),
            "output_shape": _extract_shape(off_metadata),
        },
        "skip_on": {
            "bandon_crop_skip_enabled": bool(on_metadata.get("bandon_crop_skip_enabled")),
            "crop_count_total": on_crop_count_total,
            "forward_call_count": on_forward_count,
            "crop_count_forwarded": on_forward_count,
            "crop_count_skipped_before_forward": on_skipped,
            "crop_skip_reason_counts": on_metadata.get("crop_skip_reason_counts") or {},
            "min_valid_ratio_within_aoi": on_metadata.get("min_valid_ratio_within_aoi"),
            "forward_duration_ms": _find_first_stage_duration(on_timing, "inference.bandon.forward", "forward"),
            "output_shape": _extract_shape(on_metadata),
        },
        "invariants": {
            "skip_off_forward_equals_crop_count": off_forward_count == off_crop_count,
            "skip_on_total_matches_forward_plus_skipped": on_crop_count_total == (on_forward_count + on_skipped),
            "skip_on_forward_equals_forwarded": on_forward_count == int(on_metadata.get("crop_count_forwarded") or 0),
            "output_shape_equal": _extract_shape(off_metadata) == _extract_shape(on_metadata),
        },
    }

    off_forward_duration = report["skip_off"]["forward_duration_ms"]
    on_forward_duration = report["skip_on"]["forward_duration_ms"]
    report["comparison"] = {
        "forward_call_reduction_percent": _format_percent(float(off_forward_count), float(on_forward_count)),
        "forward_duration_reduction_percent": (
            _format_percent(float(off_forward_duration), float(on_forward_duration))
            if isinstance(off_forward_duration, float) and isinstance(on_forward_duration, float)
            else "n/a"
        ),
    }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
