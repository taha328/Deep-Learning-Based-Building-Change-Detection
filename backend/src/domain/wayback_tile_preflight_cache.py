from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import Settings, get_settings
from src.domain.tiling import tile_range_for_bbox
from src.domain.wayback import TileAvailabilitySummary, WaybackRelease


WAYBACK_TILE_PREFLIGHT_CACHE_SCHEMA_VERSION = 1
WAYBACK_TILE_PREFLIGHT_CACHE_FUNCTION_VERSION = 1
WAYBACK_TILE_PREFLIGHT_CACHE_KIND = "wayback_tile_preflight"
WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_SUFFIX = ".lock"
WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_TIMEOUT_SEC = 120.0
WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_POLL_INTERVAL_SEC = 0.1
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaybackTilePreflightCacheInfo:
    cache_key: str
    cache_path: Path
    cache_enabled: bool
    cache_path_exists: bool
    cache_hit: bool = False


@dataclass(frozen=True)
class StaleWaybackTilePreflightLockCleanupResult:
    examined_count: int
    deleted_paths: tuple[Path, ...]
    skipped: tuple[dict[str, str], ...]


class WaybackTilePreflightCacheLockTimeout(TimeoutError):
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        super().__init__(f"Timed out waiting for Wayback tile preflight cache lock {lock_path}.")


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


def wayback_tile_preflight_cache_lock_path(cache_path: Path) -> Path:
    return _cache_lock_dir(cache_path)


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
                raise WaybackTilePreflightCacheLockTimeout(lock_dir)
            time.sleep(WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_POLL_INTERVAL_SEC)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _path_age_seconds(path: Path, *, now: float | None = None) -> float | None:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return (time.time() if now is None else now) - stat_result.st_mtime


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_owner_pid(lock_path: Path) -> int | None:
    owner_pid_path = lock_path / "owner.pid"
    if owner_pid_path.exists():
        try:
            return int(owner_pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    owner_json_path = lock_path / "owner.json"
    if owner_json_path.exists():
        try:
            payload = json.loads(owner_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict):
            pid = payload.get("pid")
            if isinstance(pid, int):
                return pid
            if isinstance(pid, str) and pid.isdigit():
                return int(pid)
    return None


def _skip_lock(lock_path: Path, reason: str, skipped: list[dict[str, str]]) -> None:
    skipped.append({"path": str(lock_path), "reason": reason})
    LOGGER.info("PREFLIGHT_STALE_LOCK_SKIPPED lockPath=%s reason=%s", lock_path, reason)


def _wayback_preflight_lock_cleanup_result(
    *,
    cache_dir: Path,
    trigger: str,
    source: str,
    deleted_paths: list[Path],
    failed_paths: list[dict[str, str]],
    skipped: bool = False,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cache_dir": str(cache_dir),
        "trigger": trigger,
        "source": source,
        "deleted_count": len(deleted_paths),
        "failed_count": len(failed_paths),
        "deleted_paths": [str(path) for path in deleted_paths],
        "failed_paths": failed_paths,
        "skipped": skipped,
    }
    if skip_reason is not None:
        result["skip_reason"] = skip_reason
    return result


def _log_wayback_preflight_lock_cleanup_warning(
    *,
    cache_dir: Path,
    trigger: str,
    source: str,
    reason: str,
    failed_paths: list[dict[str, str]],
    lock_path: Path | None = None,
    error: BaseException | None = None,
) -> None:
    failure = {
        "path": str(lock_path) if lock_path is not None else str(cache_dir),
        "reason": reason,
    }
    if error is not None:
        failure["error"] = f"{type(error).__name__}: {error}"
    failed_paths.append(failure)
    LOGGER.warning(
        "WAYBACK_PREFLIGHT_LOCK_CLEANUP_WARNING cache_dir=%s lock_path=%s trigger=%s source=%s reason=%s error=%s",
        cache_dir,
        lock_path,
        trigger,
        source,
        reason,
        failure.get("error"),
    )


def cleanup_wayback_preflight_locks(
    trigger: str,
    *,
    settings: Settings | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    source_value = source or trigger
    cache_dir = resolved_settings.wayback_tile_preflight_cache_dir.expanduser().resolve()
    deleted_paths: list[Path] = []
    failed_paths: list[dict[str, str]] = []

    LOGGER.info(
        "WAYBACK_PREFLIGHT_LOCK_CLEANUP_START cache_dir=%s trigger=%s source=%s",
        cache_dir,
        trigger,
        source_value,
    )
    if not cache_dir.exists():
        LOGGER.info(
            "WAYBACK_PREFLIGHT_LOCK_CLEANUP_DONE cache_dir=%s trigger=%s source=%s deleted_count=0 failed_count=0 skipped=%s reason=%s",
            cache_dir,
            trigger,
            source_value,
            True,
            "cache_dir_missing",
        )
        return _wayback_preflight_lock_cleanup_result(
            cache_dir=cache_dir,
            trigger=trigger,
            source=source_value,
            deleted_paths=deleted_paths,
            failed_paths=failed_paths,
            skipped=True,
            skip_reason="cache_dir_missing",
        )
    if not cache_dir.is_dir():
        _log_wayback_preflight_lock_cleanup_warning(
            cache_dir=cache_dir,
            trigger=trigger,
            source=source_value,
            reason="cache_dir_not_directory",
            failed_paths=failed_paths,
        )
        LOGGER.info(
            "WAYBACK_PREFLIGHT_LOCK_CLEANUP_DONE cache_dir=%s trigger=%s source=%s deleted_count=%s failed_count=%s skipped=%s reason=%s",
            cache_dir,
            trigger,
            source_value,
            len(deleted_paths),
            len(failed_paths),
            True,
            "cache_dir_not_directory",
        )
        return _wayback_preflight_lock_cleanup_result(
            cache_dir=cache_dir,
            trigger=trigger,
            source=source_value,
            deleted_paths=deleted_paths,
            failed_paths=failed_paths,
            skipped=True,
            skip_reason="cache_dir_not_directory",
        )

    try:
        lock_paths = sorted(cache_dir.rglob(f"*{WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_SUFFIX}"))
    except OSError as exc:
        _log_wayback_preflight_lock_cleanup_warning(
            cache_dir=cache_dir,
            trigger=trigger,
            source=source_value,
            reason="cache_dir_scan_failed",
            failed_paths=failed_paths,
            error=exc,
        )
        LOGGER.info(
            "WAYBACK_PREFLIGHT_LOCK_CLEANUP_DONE cache_dir=%s trigger=%s source=%s deleted_count=%s failed_count=%s",
            cache_dir,
            trigger,
            source_value,
            len(deleted_paths),
            len(failed_paths),
        )
        return _wayback_preflight_lock_cleanup_result(
            cache_dir=cache_dir,
            trigger=trigger,
            source=source_value,
            deleted_paths=deleted_paths,
            failed_paths=failed_paths,
        )

    for lock_path in lock_paths:
        if not lock_path.name.endswith(WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_SUFFIX):
            continue
        try:
            lock_path.lstat()
            resolved_lock_path = lock_path.resolve()
        except OSError as exc:
            _log_wayback_preflight_lock_cleanup_warning(
                cache_dir=cache_dir,
                lock_path=lock_path,
                trigger=trigger,
                source=source_value,
                reason="lock_path_stat_failed",
                failed_paths=failed_paths,
                error=exc,
            )
            continue

        if not _is_relative_to(resolved_lock_path, cache_dir):
            _log_wayback_preflight_lock_cleanup_warning(
                cache_dir=cache_dir,
                lock_path=lock_path,
                trigger=trigger,
                source=source_value,
                reason="outside_cache_dir",
                failed_paths=failed_paths,
            )
            continue

        try:
            if lock_path.is_symlink() or lock_path.is_file():
                lock_path.unlink()
            elif lock_path.is_dir():
                shutil.rmtree(lock_path)
            else:
                _log_wayback_preflight_lock_cleanup_warning(
                    cache_dir=cache_dir,
                    lock_path=lock_path,
                    trigger=trigger,
                    source=source_value,
                    reason="unsupported_lock_path_type",
                    failed_paths=failed_paths,
                )
                continue
        except OSError as exc:
            _log_wayback_preflight_lock_cleanup_warning(
                cache_dir=cache_dir,
                lock_path=lock_path,
                trigger=trigger,
                source=source_value,
                reason="lock_path_delete_failed",
                failed_paths=failed_paths,
                error=exc,
            )
            continue

        deleted_paths.append(resolved_lock_path)
        LOGGER.info(
            "WAYBACK_PREFLIGHT_LOCK_CLEANUP_DELETED cache_dir=%s lock_path=%s trigger=%s source=%s",
            cache_dir,
            resolved_lock_path,
            trigger,
            source_value,
        )

    LOGGER.info(
        "WAYBACK_PREFLIGHT_LOCK_CLEANUP_DONE cache_dir=%s trigger=%s source=%s deleted_count=%s failed_count=%s",
        cache_dir,
        trigger,
        source_value,
        len(deleted_paths),
        len(failed_paths),
    )
    return _wayback_preflight_lock_cleanup_result(
        cache_dir=cache_dir,
        trigger=trigger,
        source=source_value,
        deleted_paths=deleted_paths,
        failed_paths=failed_paths,
    )


def cleanup_stale_wayback_tile_preflight_locks(
    cache_dir: Path,
    *,
    stale_seconds: float,
    allow_delete_when_cache_exists: bool = False,
    now: float | None = None,
    dry_run: bool = False,
) -> StaleWaybackTilePreflightLockCleanupResult:
    resolved_cache_dir = cache_dir.expanduser().resolve()
    deleted_paths: list[Path] = []
    skipped: list[dict[str, str]] = []
    examined_count = 0

    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be greater than 0.")
    if not resolved_cache_dir.exists():
        return StaleWaybackTilePreflightLockCleanupResult(
            examined_count=0,
            deleted_paths=(),
            skipped=(),
        )

    for lock_path in resolved_cache_dir.glob("*.json.lock"):
        examined_count += 1
        resolved_lock_path = lock_path.resolve()
        if not _is_relative_to(resolved_lock_path, resolved_cache_dir):
            _skip_lock(lock_path, "outside_cache_dir", skipped)
            continue
        if not resolved_lock_path.name.endswith(".json.lock"):
            _skip_lock(lock_path, "not_preflight_cache_lock", skipped)
            continue

        cache_path = resolved_lock_path.with_name(resolved_lock_path.name[: -len(WAYBACK_TILE_PREFLIGHT_CACHE_LOCK_SUFFIX)])
        if cache_path.exists() and not allow_delete_when_cache_exists:
            _skip_lock(resolved_lock_path, "matching_cache_exists", skipped)
            continue

        age_seconds = _path_age_seconds(resolved_lock_path, now=now)
        if age_seconds is None:
            _skip_lock(resolved_lock_path, "missing", skipped)
            continue
        if age_seconds <= stale_seconds:
            _skip_lock(resolved_lock_path, "fresh", skipped)
            continue

        owner_pid = _lock_owner_pid(resolved_lock_path)
        if owner_pid is not None and _pid_exists(owner_pid):
            _skip_lock(resolved_lock_path, "owner_alive", skipped)
            continue

        if not dry_run:
            if resolved_lock_path.is_dir():
                shutil.rmtree(resolved_lock_path)
            else:
                resolved_lock_path.unlink(missing_ok=True)
        deleted_paths.append(resolved_lock_path)
        LOGGER.info(
            "PREFLIGHT_STALE_LOCK_DELETED lockPath=%s ageSeconds=%.3f dryRun=%s",
            resolved_lock_path,
            age_seconds,
            dry_run,
        )

    return StaleWaybackTilePreflightLockCleanupResult(
        examined_count=examined_count,
        deleted_paths=tuple(deleted_paths),
        skipped=tuple(skipped),
    )


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
