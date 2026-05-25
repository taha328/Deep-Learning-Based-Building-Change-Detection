from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


REQUIRED_EVENTS = {
    "TEMPORAL_VECTOR_TILE_SOURCE_REGISTERED",
    "TEMPORAL_VECTOR_TILE_LAYER_REGISTERED",
    "TEMPORAL_GEOJSON_FETCH_SKIPPED_HUGE_ARTIFACT",
    "TEMPORAL_EMPTY_BASELINE_LAYER_SKIPPED",
    "TEMPORAL_OUTPUT_LAYER_SYNC_DONE",
}

FORBIDDEN_MARKERS = (
    "payloadBytes=1217791800",
    "payloadBytes=1217716451",
    "registeredLayerCount=34",
    "expectedOutputLayerCount=34",
    "temporalLayerCount=34",
    "WB_2020_R04-additions",
)

FORBIDDEN_LAZY_ARTIFACTS = {
    "additions",
    "building_change_buffer_10m",
    "building_change_buffer_15m",
    "building_change_buffer_20m",
}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_events(path: Path, project_id: str, since_minutes: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        received_at = _parse_timestamp(event.get("received_at"))
        if received_at and received_at < cutoff:
            continue
        payload = event.get("payload") or {}
        serialized = json.dumps(event, ensure_ascii=False, default=str)
        if payload.get("projectId") == project_id or project_id in serialized:
            events.append(event)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit frontend temporal vector-tile runtime client logs.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--log-path",
        default="runtime_cache/dev_client_logs/client_log.ndjson",
        help="Path relative to backend/ or absolute path to persisted dev client logs.",
    )
    parser.add_argument("--since-minutes", type=int, default=120)
    args = parser.parse_args()

    log_path = Path(args.log_path)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parents[1] / log_path

    events = _load_events(log_path, args.project_id, args.since_minutes)
    event_names = {event.get("event") for event in events}
    missing_events = sorted(REQUIRED_EVENTS - event_names)
    serialized_events = "\n".join(json.dumps(event, ensure_ascii=False, default=str) for event in events)
    forbidden_markers = [marker for marker in FORBIDDEN_MARKERS if marker in serialized_events]
    forbidden_lazy_fetches = []
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event") == "PROJECT_LAYER_ARTIFACT_LAZY_FETCH" and payload.get("artifactKey") in FORBIDDEN_LAZY_ARTIFACTS:
            forbidden_lazy_fetches.append(payload)
    largest_payload_bytes = 0
    for event in events:
        payload = event.get("payload") or {}
        value = payload.get("payloadBytes")
        if isinstance(value, (int, float)):
            largest_payload_bytes = max(largest_payload_bytes, int(value))

    result = {
        "project_id": args.project_id,
        "log_path": str(log_path),
        "event_count": len(events),
        "events": sorted(str(name) for name in event_names if name),
        "missing_required_events": missing_events,
        "forbidden_markers": forbidden_markers,
        "forbidden_lazy_fetches": forbidden_lazy_fetches,
        "largest_payload_bytes": largest_payload_bytes,
        "accepted": not missing_events and not forbidden_markers and not forbidden_lazy_fetches,
    }
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
