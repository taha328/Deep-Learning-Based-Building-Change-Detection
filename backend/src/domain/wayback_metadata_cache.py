from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import Settings
from src.domain.wayback import MetadataSummary, metadata_base_url_from_identifier


WAYBACK_METADATA_CACHE_SCHEMA_VERSION = 1
WAYBACK_METADATA_CACHE_FUNCTION_VERSION = 1
WAYBACK_METADATA_CACHE_KIND = "wayback_metadata_summary"
WAYBACK_METADATA_CACHE_LOCK_SUFFIX = ".lock"
WAYBACK_METADATA_CACHE_LOCK_TIMEOUT_SEC = 120.0
WAYBACK_METADATA_CACHE_LOCK_POLL_INTERVAL_SEC = 0.1


@dataclass(frozen=True)
class WaybackMetadataCacheInfo:
    cache_key: str
    cache_path: Path
    cache_enabled: bool
    cache_path_exists: bool
    cache_hit: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_bbox(bbox: dict[str, float]) -> dict[str, float]:
    return {
        "west": round(float(bbox["west"]), 8),
        "south": round(float(bbox["south"]), 8),
        "east": round(float(bbox["east"]), 8),
        "north": round(float(bbox["north"]), 8),
    }


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def build_wayback_metadata_cache_key(
    settings: Settings,
    *,
    release_identifier: str,
    release_date: str | None,
    bbox: dict[str, float],
    aoi_geojson: dict[str, Any] | None,
    zoom: int,
) -> str:
    cache_inputs = {
        "cache_schema_version": WAYBACK_METADATA_CACHE_SCHEMA_VERSION,
        "cache_function": "summarize_wayback_metadata",
        "cache_function_version": WAYBACK_METADATA_CACHE_FUNCTION_VERSION,
        "release_identifier": release_identifier,
        "release_date": release_date,
        "metadata_base_url": metadata_base_url_from_identifier(release_identifier),
        "wmts_capabilities_url": settings.wmts_capabilities_url,
        "tile_matrix_set": settings.tile_matrix_set,
        "zoom": zoom,
        "grid_size": settings.metadata_grid_size,
        "bbox": _normalize_bbox(bbox),
        "aoi_geojson_sha256": _stable_hash(aoi_geojson or bbox),
    }
    digest = hashlib.sha256(_canonical_json(cache_inputs).encode("utf-8")).hexdigest()
    return f"wayback-metadata-{digest}"


def get_wayback_metadata_cache_path(settings: Settings, cache_key: str) -> Path:
    return settings.wayback_metadata_cache_dir / f"{cache_key}.json"


def _cache_lock_dir(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}{WAYBACK_METADATA_CACHE_LOCK_SUFFIX}")


@contextmanager
def acquire_wayback_metadata_cache_lock(cache_path: Path) -> Iterator[None]:
    lock_dir = _cache_lock_dir(cache_path)
    deadline = time.monotonic() + WAYBACK_METADATA_CACHE_LOCK_TIMEOUT_SEC
    while True:
        try:
            lock_dir.mkdir(parents=False)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Wayback metadata cache lock {lock_dir}.")
            time.sleep(WAYBACK_METADATA_CACHE_LOCK_POLL_INTERVAL_SEC)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _parse_expiry(expires_at: str | None) -> datetime | None:
    if not expires_at:
        return None
    try:
        normalized = expires_at.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _metadata_summary_from_payload(summary_payload: dict[str, Any]) -> MetadataSummary | None:
    try:
        return MetadataSummary(
            dominant_src_date=summary_payload.get("dominant_src_date"),
            dominant_src_res_m=summary_payload.get("dominant_src_res_m"),
            metadata_region_count=int(summary_payload.get("metadata_region_count", 0)),
            capture_date_count=int(summary_payload.get("capture_date_count", 0)),
            mixed_capture_dates=bool(summary_payload.get("mixed_capture_dates", False)),
            metadata_coverage_fraction=summary_payload.get("metadata_coverage_fraction"),
        )
    except (TypeError, ValueError):
        return None


def read_wayback_metadata_cache(
    cache_path: Path,
    *,
    cache_key: str,
    ttl_seconds: int,
) -> MetadataSummary | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache_path.unlink(missing_ok=True)
        return None
    if not isinstance(payload, dict):
        cache_path.unlink(missing_ok=True)
        return None
    if payload.get("schema_version") != WAYBACK_METADATA_CACHE_SCHEMA_VERSION:
        cache_path.unlink(missing_ok=True)
        return None
    if payload.get("cache_type") != WAYBACK_METADATA_CACHE_KIND:
        cache_path.unlink(missing_ok=True)
        return None
    if payload.get("cache_key") != cache_key:
        cache_path.unlink(missing_ok=True)
        return None

    created_at_raw = payload.get("created_at")
    expires_at_raw = payload.get("expires_at")
    if not isinstance(created_at_raw, str) or not isinstance(expires_at_raw, str):
        cache_path.unlink(missing_ok=True)
        return None

    expires_at = _parse_expiry(expires_at_raw)
    if expires_at is None:
        cache_path.unlink(missing_ok=True)
        return None
    if expires_at <= datetime.now(timezone.utc):
        cache_path.unlink(missing_ok=True)
        return None

    if ttl_seconds > 0:
        created_at = _parse_expiry(created_at_raw)
        if created_at is None:
            cache_path.unlink(missing_ok=True)
            return None
        age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
        if age_seconds > ttl_seconds:
            cache_path.unlink(missing_ok=True)
            return None

    summary_payload = payload.get("summary")
    if not isinstance(summary_payload, dict):
        cache_path.unlink(missing_ok=True)
        return None
    return _metadata_summary_from_payload(summary_payload)


def build_wayback_metadata_cache_payload(
    *,
    settings: Settings,
    cache_key: str,
    release_identifier: str,
    release_date: str | None,
    bbox: dict[str, float],
    aoi_geojson: dict[str, Any] | None,
    zoom: int,
    summary: MetadataSummary,
    ttl_seconds: int,
) -> dict[str, Any]:
    created_at = _utc_now_iso()
    expiry_iso = (datetime.now(timezone.utc) + timedelta(seconds=max(ttl_seconds, 0))).isoformat().replace(
        "+00:00", "Z"
    )
    return {
        "schema_version": WAYBACK_METADATA_CACHE_SCHEMA_VERSION,
        "cache_type": WAYBACK_METADATA_CACHE_KIND,
        "cache_function": "summarize_wayback_metadata",
        "cache_function_version": WAYBACK_METADATA_CACHE_FUNCTION_VERSION,
        "cache_key": cache_key,
        "created_at": created_at,
        "expires_at": expiry_iso,
        "inputs": {
            "release_identifier": release_identifier,
            "release_date": release_date,
            "metadata_base_url": metadata_base_url_from_identifier(release_identifier),
            "wmts_capabilities_url": settings.wmts_capabilities_url,
            "tile_matrix_set": settings.tile_matrix_set,
            "zoom": zoom,
            "grid_size": settings.metadata_grid_size,
            "bbox": _normalize_bbox(bbox),
            "aoi_geojson_sha256": _stable_hash(aoi_geojson or bbox),
        },
        "summary": asdict(summary),
    }


def write_wayback_metadata_cache_atomic(cache_path: Path, payload: dict[str, Any]) -> Path:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(cache_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return cache_path
