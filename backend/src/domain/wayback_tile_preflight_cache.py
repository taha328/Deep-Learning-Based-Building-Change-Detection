from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import Settings
from src.domain.tiling import tile_range_for_bbox
from src.domain.wayback import TileAvailabilitySummary, WaybackRelease


WAYBACK_TILE_PREFLIGHT_CACHE_SCHEMA_VERSION = 1
WAYBACK_TILE_PREFLIGHT_CACHE_FUNCTION_VERSION = 1
WAYBACK_TILE_PREFLIGHT_CACHE_KIND = "wayback_tile_preflight"
WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_SUFFIX = ".lock"
WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_TIMEOUT_SEC = 120.0
WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_POLL_INTERVAL_SEC = 0.1


@dataclass(frozen=True)
class WaybackTilePreflightCacheInfo:
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


def _tilemap_base_url(release: WaybackRelease) -> str:
    return release.resource_url_template.split("/tile/")[0] + "/tilemap"


def build_wayback_tile_preflight_cache_key(
    settings: Settings,
    *,
    release: WaybackRelease,
    bbox: dict[str, float],
    aoi_geojson: dict[str, Any] | None,
    zoom: int,
) -> str:
    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
    tile_range = {
        "x_min": x_min,
        "x_max": x_max,
        "y_min": y_min,
        "y_max": y_max,
    }
    cache_inputs = {
        "cache_schema_version": WAYBACK_TILE_PREFLIGHT_CACHE_SCHEMA_VERSION,
        "cache_function": "preflight_wayback_tile_availability",
        "cache_function_version": WAYBACK_TILE_PREFLIGHT_CACHE_FUNCTION_VERSION,
        "release_identifier": release.identifier,
        "release_date": str(release.release_date),
        "release_num": release.release_num,
        "wmts_capabilities_url": settings.wmts_capabilities_url,
        "tile_service_url": release.resource_url_template,
        "tilemap_base_url": _tilemap_base_url(release),
        "tile_matrix_set": settings.tile_matrix_set,
        "release_tile_matrix_sets": list(release.tile_matrix_sets),
        "zoom": zoom,
        "bbox": _normalize_bbox(bbox),
        "tile_range": tile_range,
        "aoi_geojson_sha256": _stable_hash(aoi_geojson or bbox),
        "tile_range_sha256": _stable_hash(tile_range),
    }
    digest = hashlib.sha256(_canonical_json(cache_inputs).encode("utf-8")).hexdigest()
    return f"wayback-tile-preflight-{digest}"


def get_wayback_tile_preflight_cache_path(settings: Settings, cache_key: str) -> Path:
    return settings.wayback_tile_preflight_cache_dir / f"{cache_key}.json"


def _cache_lock_dir(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}{WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_SUFFIX}")


@contextmanager
def acquire_wayback_tile_preflight_cache_lock(cache_path: Path) -> Iterator[None]:
    lock_dir = _cache_lock_dir(cache_path)
    deadline = time.monotonic() + WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_TIMEOUT_SEC
    while True:
        try:
            lock_dir.mkdir(parents=False)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Wayback tile preflight cache lock {lock_dir}.")
            time.sleep(WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_POLL_INTERVAL_SEC)
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


def _tile_summary_from_payload(summary_payload: dict[str, Any]) -> TileAvailabilitySummary | None:
    available_tiles_payload = summary_payload.get("available_tiles")
    if not isinstance(available_tiles_payload, list):
        return None
    try:
        available_tiles = frozenset(
            (int(tile[0]), int(tile[1]))
            for tile in available_tiles_payload
            if isinstance(tile, list) and len(tile) == 2
        )
        return TileAvailabilitySummary(
            candidate_count=int(summary_payload.get("candidate_count", 0)),
            available_count=int(summary_payload.get("available_count", 0)),
            missing_count=int(summary_payload.get("missing_count", 0)),
            failed_check_count=int(summary_payload.get("failed_check_count", 0)),
            preflight_complete=bool(summary_payload.get("preflight_complete", False)),
            availability_fraction=(
                float(summary_payload["availability_fraction"])
                if summary_payload.get("availability_fraction") is not None
                else None
            ),
            available_tiles=available_tiles,
        )
    except (TypeError, ValueError):
        return None


def read_wayback_tile_preflight_cache(
    cache_path: Path,
    *,
    cache_key: str,
    ttl_seconds: int,
) -> TileAvailabilitySummary | None:
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
    if payload.get("schema_version") != WAYBACK_TILE_PREFLIGHT_CACHE_SCHEMA_VERSION:
        cache_path.unlink(missing_ok=True)
        return None
    if payload.get("cache_type") != WAYBACK_TILE_PREFLIGHT_CACHE_KIND:
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
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
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

    summary_payload = payload.get("preflight")
    if not isinstance(summary_payload, dict):
        cache_path.unlink(missing_ok=True)
        return None
    summary = _tile_summary_from_payload(summary_payload)
    if summary is None:
        cache_path.unlink(missing_ok=True)
        return None
    return summary


def build_wayback_tile_preflight_cache_payload(
    *,
    settings: Settings,
    cache_key: str,
    release: WaybackRelease,
    bbox: dict[str, float],
    aoi_geojson: dict[str, Any] | None,
    zoom: int,
    tilemap: TileAvailabilitySummary,
    ttl_seconds: int,
) -> dict[str, Any]:
    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
    tile_range = {
        "x_min": x_min,
        "x_max": x_max,
        "y_min": y_min,
        "y_max": y_max,
    }
    created_at = _utc_now_iso()
    expiry_iso = (datetime.now(timezone.utc) + timedelta(seconds=max(ttl_seconds, 0))).isoformat().replace(
        "+00:00", "Z"
    )
    return {
        "schema_version": WAYBACK_TILE_PREFLIGHT_CACHE_SCHEMA_VERSION,
        "cache_type": WAYBACK_TILE_PREFLIGHT_CACHE_KIND,
        "cache_function": "preflight_wayback_tile_availability",
        "cache_function_version": WAYBACK_TILE_PREFLIGHT_CACHE_FUNCTION_VERSION,
        "cache_key": cache_key,
        "created_at": created_at,
        "expires_at": expiry_iso,
        "inputs": {
            "release_identifier": release.identifier,
            "release_date": str(release.release_date),
            "release_num": release.release_num,
            "wmts_capabilities_url": settings.wmts_capabilities_url,
            "tile_service_url": release.resource_url_template,
            "tilemap_base_url": _tilemap_base_url(release),
            "tile_matrix_set": settings.tile_matrix_set,
            "release_tile_matrix_sets": list(release.tile_matrix_sets),
            "zoom": zoom,
            "bbox": _normalize_bbox(bbox),
            "tile_range": tile_range,
            "aoi_geojson_sha256": _stable_hash(aoi_geojson or bbox),
            "tile_range_sha256": _stable_hash(tile_range),
        },
        "preflight": {
            "candidate_count": tilemap.candidate_count,
            "available_count": tilemap.available_count,
            "missing_count": tilemap.missing_count,
            "failed_check_count": tilemap.failed_check_count,
            "preflight_complete": tilemap.preflight_complete,
            "availability_fraction": tilemap.availability_fraction,
            "available_tiles": [[x, y] for x, y in sorted(tilemap.available_tiles)],
        },
    }


def write_wayback_tile_preflight_cache_atomic(cache_path: Path, payload: dict[str, Any]) -> Path:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(cache_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return cache_path
