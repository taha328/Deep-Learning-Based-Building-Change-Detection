from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Iterator, Literal


StageStatus = Literal["success", "failed"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(start_ns: int, end_ns: int) -> float:
    return round((end_ns - start_ns) / 1_000_000, 2)


def sanitize_metadata_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): sanitize_metadata_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_metadata_value(item) for item in value]
    return str(value)


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_metadata_value(value) for key, value in metadata.items()}


@dataclass
class StageTimingEntry:
    name: str
    started_at: str
    completed_at: str
    duration_ms: float
    status: StageStatus
    metadata: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "metadata": self.metadata,
        }
        if self.error_type:
            payload["error_type"] = self.error_type
        return payload


class StageTimingRecorder:
    def __init__(
        self,
        *,
        run_id: str,
        pipeline_kind: str,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.pipeline_kind = pipeline_kind
        self.project_id = project_id
        self.metadata = _safe_metadata(metadata or {})
        self.started_at = _utc_now()
        self._started_ns = time.perf_counter_ns()
        self._completed_at: str | None = None
        self._completed_ns: int | None = None
        self._stages: list[StageTimingEntry] = []

    @contextmanager
    def stage(self, name: str, **metadata: Any) -> Iterator[None]:
        started_at = _utc_now()
        started_ns = time.perf_counter_ns()
        status: StageStatus = "success"
        error_type: str | None = None
        try:
            yield
        except Exception as exc:
            status = "failed"
            error_type = type(exc).__name__
            raise
        finally:
            completed_ns = time.perf_counter_ns()
            self._stages.append(
                StageTimingEntry(
                    name=name,
                    started_at=started_at,
                    completed_at=_utc_now(),
                    duration_ms=_duration_ms(started_ns, completed_ns),
                    status=status,
                    metadata=_safe_metadata(metadata),
                    error_type=error_type,
                )
            )

    def add_stage(
        self,
        name: str,
        *,
        duration_ms: float,
        status: StageStatus = "success",
        metadata: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None:
        now = _utc_now()
        self._stages.append(
            StageTimingEntry(
                name=name,
                started_at=now,
                completed_at=now,
                duration_ms=round(float(duration_ms), 2),
                status=status,
                metadata=_safe_metadata(metadata or {}),
                error_type=error_type,
            )
        )

    def merge_child_timings(self, child: "StageTimingRecorder | dict[str, Any]", *, prefix: str | None = None) -> None:
        payload = child.to_dict() if isinstance(child, StageTimingRecorder) else child
        child_run_id = payload.get("run_id")
        for raw_stage in payload.get("stages", []):
            if not isinstance(raw_stage, dict):
                continue
            raw_name = raw_stage.get("name")
            duration_ms = raw_stage.get("duration_ms")
            status = raw_stage.get("status")
            if not isinstance(raw_name, str) or not isinstance(duration_ms, (int, float)):
                continue
            name = f"{prefix}.{raw_name}" if prefix else raw_name
            metadata = raw_stage.get("metadata") if isinstance(raw_stage.get("metadata"), dict) else {}
            if isinstance(child_run_id, str):
                metadata = {**metadata, "child_run_id": child_run_id}
            self.add_stage(
                name,
                duration_ms=float(duration_ms),
                status="failed" if status == "failed" else "success",
                metadata=metadata,
                error_type=raw_stage.get("error_type") if isinstance(raw_stage.get("error_type"), str) else None,
            )

    def complete(self) -> None:
        if self._completed_ns is None:
            self._completed_ns = time.perf_counter_ns()
            self._completed_at = _utc_now()

    def to_dict(self) -> dict[str, Any]:
        completed_ns = self._completed_ns or time.perf_counter_ns()
        completed_at = self._completed_at or _utc_now()
        slowest = max(self._stages, key=lambda item: item.duration_ms, default=None)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "pipeline_kind": self.pipeline_kind,
            "started_at": self.started_at,
            "completed_at": completed_at,
            "total_runtime_ms": _duration_ms(self._started_ns, completed_ns),
            "stages": [stage.to_dict() for stage in self._stages],
            "summary": {
                "slowest_stage": slowest.name if slowest else None,
                "slowest_stage_ms": slowest.duration_ms if slowest else None,
            },
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload

    def write_timing_report(self, path: Path) -> Path:
        self.complete()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path
