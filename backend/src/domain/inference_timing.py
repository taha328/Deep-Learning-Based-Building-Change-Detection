from __future__ import annotations

from contextlib import contextmanager
import json
import statistics
import time
from pathlib import Path
from typing import Any, Iterator


class TimingValue:
    def __init__(self) -> None:
        self.ms: float = 0.0


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


@contextmanager
def timed_block() -> Iterator[TimingValue]:
    value = TimingValue()
    start = time.perf_counter()
    try:
        yield value
    finally:
        value.ms = elapsed_ms(start)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if fraction <= 0:
        return min(values)
    if fraction >= 1:
        return max(values)
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * weight)


def summary_statistics(values: list[float]) -> dict[str, float | int | None]:
    finite = [float(value) for value in values if isinstance(value, (int, float))]
    if not finite:
        return {
            "count": 0,
            "mean_ms": None,
            "median_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "min_ms": None,
            "total_ms": 0.0,
        }
    return {
        "count": len(finite),
        "mean_ms": round(statistics.fmean(finite), 3),
        "median_ms": round(statistics.median(finite), 3),
        "p90_ms": round(percentile(finite, 0.90), 3),
        "p95_ms": round(percentile(finite, 0.95), 3),
        "max_ms": round(max(finite), 3),
        "min_ms": round(min(finite), 3),
        "total_ms": round(sum(finite), 3),
    }


def aggregate_timing_records(records: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, float | int | None]]:
    summary: dict[str, dict[str, float | int | None]] = {}
    for field in fields:
        values = [record[field] for record in records if isinstance(record.get(field), (int, float))]
        summary[field] = summary_statistics(values)
    return summary


def safe_merge_json_file(path: Path, payload: dict[str, Any]) -> bool:
    try:
        existing: dict[str, Any] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**existing, **payload}, indent=2, sort_keys=True), encoding="utf-8")
        return True
    except Exception:
        return False


def write_timing_summary(
    path: Path,
    *,
    run_id: str,
    records: list[dict[str, Any]],
    fields: list[str],
) -> bool:
    payload = {
        "run_id": run_id,
        "record_count": len(records),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": aggregate_timing_records(records, fields),
    }
    return safe_merge_json_file(path, payload)
