from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import Settings
from src.domain.wayback import WaybackRelease, build_session, parse_wmts_capabilities
from src.domain.wayback_release_cache import (
    WaybackReleaseCachePayload,
    build_wayback_release_cache_payload,
    read_wayback_release_cache,
    write_wayback_release_cache_atomic,
)
from src.schemas import ReleaseListResponse, ReleaseMetadata


logger = logging.getLogger(__name__)

_RELEASES_CACHE_KEY_SCHEMA = "wayback-releases-v1"
_memory_cache_lock = Lock()
_memory_cache: dict[str, "CachedReleasesSnapshot"] = {}


@dataclass(frozen=True)
class ReleaseServiceError(Exception):
    code: str
    message: str
    details: dict[str, object]

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class CachedReleasesSnapshot:
    releases: tuple[WaybackRelease, ...]
    fetched_at: datetime
    source_status: str
    warnings: tuple[dict[str, object], ...] = ()


def _memory_cache_key(settings: Settings) -> str:
    return "|".join(
        (
            _RELEASES_CACHE_KEY_SCHEMA,
            settings.wmts_capabilities_url,
            settings.tile_matrix_set,
        )
    )


def _is_fresh(fetched_at: datetime, ttl_seconds: int) -> bool:
    return datetime.now(UTC) - fetched_at <= timedelta(seconds=max(ttl_seconds, 0))


def _build_warning() -> dict[str, object]:
    return {
        "code": "wayback_releases_stale_fallback",
        "severity": "warning",
        "message": "Using cached Esri Wayback releases because the live WMTS capabilities endpoint is temporarily unreachable.",
    }


def _build_release_session(settings: Settings) -> requests.Session:
    session = build_session(settings)
    session.request_timeout_sec = (  # type: ignore[attr-defined]
        settings.wayback_releases_connect_timeout_seconds,
        settings.wayback_releases_read_timeout_seconds,
    )
    retry = Retry(
        total=settings.wayback_releases_retries,
        connect=settings.wayback_releases_retries,
        read=settings.wayback_releases_retries,
        status=settings.wayback_releases_retries,
        backoff_factor=settings.wayback_releases_retry_backoff_seconds,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _load_memory_cache(settings: Settings) -> CachedReleasesSnapshot | None:
    if not settings.wayback_releases_cache_enabled:
        return None
    cache_key = _memory_cache_key(settings)
    with _memory_cache_lock:
        snapshot = _memory_cache.get(cache_key)
    if snapshot is None:
        return None
    if _is_fresh(snapshot.fetched_at, settings.wayback_releases_cache_ttl_seconds):
        return snapshot
    return None


def _store_memory_cache(settings: Settings, snapshot: CachedReleasesSnapshot) -> None:
    if not settings.wayback_releases_cache_enabled:
        return
    with _memory_cache_lock:
        _memory_cache[_memory_cache_key(settings)] = snapshot


def _load_disk_cache(settings: Settings) -> WaybackReleaseCachePayload | None:
    if not settings.wayback_releases_cache_enabled:
        return None
    return read_wayback_release_cache(settings.wayback_releases_cache_path)


def _store_disk_cache(settings: Settings, releases: list[WaybackRelease], fetched_at: datetime) -> None:
    if not settings.wayback_releases_cache_enabled:
        return
    payload = build_wayback_release_cache_payload(
        source_url=settings.wmts_capabilities_url,
        releases=releases,
        fetched_at=fetched_at,
    )
    write_wayback_release_cache_atomic(settings.wayback_releases_cache_path, payload)


def _fetch_live_releases(settings: Settings) -> CachedReleasesSnapshot:
    session = _build_release_session(settings)
    fetched_at = datetime.now(UTC)
    releases = tuple(parse_wmts_capabilities(session, settings.wmts_capabilities_url))
    snapshot = CachedReleasesSnapshot(
        releases=releases,
        fetched_at=fetched_at,
        source_status="live",
    )
    _store_memory_cache(settings, snapshot)
    _store_disk_cache(settings, list(releases), fetched_at)
    return snapshot


def _fallback_snapshot_from_disk(
    settings: Settings,
    *,
    warning: dict[str, object],
) -> CachedReleasesSnapshot | None:
    cached = _load_disk_cache(settings)
    if cached is None:
        return None
    snapshot = CachedReleasesSnapshot(
        releases=tuple(cached.releases),
        fetched_at=cached.fetched_at,
        source_status="stale",
        warnings=(warning,),
    )
    _store_memory_cache(settings, snapshot)
    return snapshot


def _fallback_snapshot_from_memory(
    settings: Settings,
    *,
    warning: dict[str, object],
) -> CachedReleasesSnapshot | None:
    if not settings.wayback_releases_cache_enabled:
        return None
    with _memory_cache_lock:
        cached = _memory_cache.get(_memory_cache_key(settings))
    if cached is None:
        return None
    return CachedReleasesSnapshot(
        releases=cached.releases,
        fetched_at=cached.fetched_at,
        source_status="stale_memory",
        warnings=(warning,),
    )


def _build_service_unavailable_error(settings: Settings) -> ReleaseServiceError:
    return ReleaseServiceError(
        code="wayback_releases_unreachable",
        message="Esri Wayback release service is temporarily unreachable. Check DNS/network and retry.",
        details={
            "source_url": settings.wmts_capabilities_url,
            "cache_available": False,
        },
    )


def _log_live_fetch_failure(exc: Exception, settings: Settings, fallback: str) -> None:
    logger.warning(
        "wayback releases live fetch failed: code=%s fallback=%s url=%s",
        exc.__class__.__name__,
        fallback,
        settings.wmts_capabilities_url,
    )


def resolve_releases_snapshot(settings: Settings) -> CachedReleasesSnapshot:
    fresh_memory = _load_memory_cache(settings)
    if fresh_memory is not None:
        return fresh_memory
    try:
        return _fetch_live_releases(settings)
    except (requests.RequestException, ValueError) as exc:
        warning = _build_warning()
        if settings.wayback_releases_stale_if_error_enabled:
            stale_disk = _fallback_snapshot_from_disk(settings, warning=warning)
            if stale_disk is not None:
                _log_live_fetch_failure(exc, settings, stale_disk.source_status)
                return stale_disk
            stale_memory = _fallback_snapshot_from_memory(settings, warning=warning)
            if stale_memory is not None:
                _log_live_fetch_failure(exc, settings, stale_memory.source_status)
                return stale_memory
        _log_live_fetch_failure(exc, settings, "none")
        raise _build_service_unavailable_error(settings) from None


def list_releases(settings: Settings) -> list[WaybackRelease]:
    return list(resolve_releases_snapshot(settings).releases)


def list_releases_response(settings: Settings) -> ReleaseListResponse:
    snapshot = resolve_releases_snapshot(settings)
    releases = [
        ReleaseMetadata(
            identifier=item.identifier,
            release_date=str(item.release_date),
            label=item.label,
            release_num=item.release_num,
        )
        for item in snapshot.releases
    ]
    return ReleaseListResponse(
        releases=releases,
        source_status=snapshot.source_status,  # type: ignore[arg-type]
        warnings=list(snapshot.warnings),
        fetched_at=snapshot.fetched_at.isoformat().replace("+00:00", "Z"),
    )
