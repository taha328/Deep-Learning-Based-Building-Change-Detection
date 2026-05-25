from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Literal


_lock = threading.RLock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
_metrics_path = Path(
    os.getenv(
        "APP_WAYBACK_METRICS_FILE",
        str(Path(__file__).resolve().parents[2] / "runtime_cache" / "wayback_metrics.jsonl"),
    )
)


def _labels(**labels: str | int | float | bool | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, "" if value is None else str(value)) for key, value in labels.items()))


def inc_counter(name: str, amount: float = 1.0, **labels: str | int | float | bool | None) -> None:
    normalized_labels = _labels(**labels)
    with _lock:
        _counters[(name, normalized_labels)] += amount
    _append_metric_event("counter", name, amount, normalized_labels)


def set_gauge(name: str, value: float, **labels: str | int | float | bool | None) -> None:
    normalized_labels = _labels(**labels)
    with _lock:
        _gauges[(name, normalized_labels)] = value
    _append_metric_event("gauge", name, value, normalized_labels)


def observe_histogram(name: str, value: float, **labels: str | int | float | bool | None) -> None:
    normalized_labels = _labels(**labels)
    with _lock:
        _histograms[(name, normalized_labels)].append(value)
    _append_metric_event("histogram", name, value, normalized_labels)


def record_tile_download(
    *,
    release: str,
    zoom: int,
    status: str,
    duration_seconds: float,
    byte_size: int = 0,
    cache_hit: bool = False,
    retry_count: int = 0,
    throttle_count: int = 0,
    timeout_count: int = 0,
) -> None:
    inc_counter("wayback_tiles_total", release=release, zoom=zoom, status=status, cache_hit=cache_hit)
    inc_counter("wayback_tile_bytes_total", byte_size, release=release, zoom=zoom, status=status)
    inc_counter("wayback_tile_retries_total", retry_count, release=release, zoom=zoom)
    inc_counter("wayback_tile_throttles_total", throttle_count, release=release, zoom=zoom)
    inc_counter("wayback_tile_timeouts_total", timeout_count, release=release, zoom=zoom)
    observe_histogram("wayback_tile_download_duration_seconds", duration_seconds, release=release, zoom=zoom, status=status)


def render_prometheus_text() -> str:
    lines: list[str] = []
    with _lock:
        snapshots = {
            "counter": dict(_counters),
            "gauge": dict(_gauges),
            "histogram": {key: list(values) for key, values in _histograms.items()},
        }
    _merge_persisted_metrics(snapshots)

    for (name, labels), value in sorted(snapshots["counter"].items()):
        lines.append(_sample_line(name, labels, value))
    for (name, labels), value in sorted(snapshots["gauge"].items()):
        lines.append(_sample_line(name, labels, value))
    for (name, labels), values in sorted(snapshots["histogram"].items()):
        if not values:
            continue
        count = len(values)
        total = sum(values)
        lines.append(_sample_line(f"{name}_count", labels, count))
        lines.append(_sample_line(f"{name}_sum", labels, total))
    return "\n".join(lines) + "\n"


def _sample_line(name: str, labels: tuple[tuple[str, str], ...], value: float) -> str:
    if labels:
        label_text = ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels)
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


MetricKind = Literal["counter", "gauge", "histogram"]


def _append_metric_event(
    kind: MetricKind,
    name: str,
    value: float,
    labels: tuple[tuple[str, str], ...],
) -> None:
    try:
        _metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with _metrics_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": kind,
                        "name": name,
                        "value": value,
                        "labels": list(labels),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError:
        return


def _merge_persisted_metrics(snapshots: dict[str, object]) -> None:
    if not _metrics_path.is_file():
        return
    counters = snapshots["counter"]
    gauges = snapshots["gauge"]
    histograms = snapshots["histogram"]
    if not isinstance(counters, dict) or not isinstance(gauges, dict) or not isinstance(histograms, dict):
        return
    try:
        for line in _metrics_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
                labels = tuple(tuple(item) for item in event.get("labels", []))
                key = (event["name"], labels)
                value = float(event.get("value", 0.0))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            kind = event.get("kind")
            if kind == "counter":
                counters[key] = float(counters.get(key, 0.0)) + value
            elif kind == "gauge":
                gauges[key] = value
            elif kind == "histogram":
                histograms.setdefault(key, []).append(value)
    except OSError:
        return
