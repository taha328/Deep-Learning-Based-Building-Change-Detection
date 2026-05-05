from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.domain.wayback import WaybackRelease


WAYBACK_RELEASES_CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WaybackReleaseCachePayload:
    fetched_at: datetime
    source_url: str
    releases: list[WaybackRelease]


def _serialize_release(release: WaybackRelease) -> dict[str, object]:
    return {
        "id": release.identifier,
        "title": release.label,
        "date": release.release_date.isoformat(),
        "release_date": release.release_date.isoformat(),
        "release_num": release.release_num,
        "tile_matrix_sets": list(release.tile_matrix_sets),
        "resource_url_template": release.resource_url_template,
    }


def _deserialize_release(item: dict[str, object]) -> WaybackRelease:
    release_date_raw = item.get("release_date") or item.get("date")
    if not isinstance(release_date_raw, str):
        raise ValueError("Cached release is missing release_date.")
    identifier = item.get("id") or item.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Cached release is missing identifier.")
    label = item.get("title") or item.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError("Cached release is missing label.")
    tile_matrix_sets_raw = item.get("tile_matrix_sets") or ()
    if not isinstance(tile_matrix_sets_raw, list | tuple):
        raise ValueError("Cached release tile_matrix_sets must be a list.")
    resource_url_template = item.get("resource_url_template")
    if not isinstance(resource_url_template, str) or not resource_url_template:
        raise ValueError("Cached release is missing resource_url_template.")
    release_num_raw = item.get("release_num")
    release_num = int(release_num_raw) if isinstance(release_num_raw, int | float) else None
    return WaybackRelease(
        identifier=identifier,
        release_date=datetime.fromisoformat(release_date_raw).date(),
        label=label,
        release_num=release_num,
        tile_matrix_sets=tuple(str(value) for value in tile_matrix_sets_raw),
        resource_url_template=resource_url_template,
    )


def build_wayback_release_cache_payload(
    *,
    source_url: str,
    releases: list[WaybackRelease],
    fetched_at: datetime | None = None,
) -> dict[str, object]:
    timestamp = (fetched_at or datetime.now(UTC)).astimezone(UTC)
    return {
        "schema_version": WAYBACK_RELEASES_CACHE_SCHEMA_VERSION,
        "fetched_at": timestamp.isoformat().replace("+00:00", "Z"),
        "source_url": source_url,
        "releases": [_serialize_release(release) for release in releases],
    }


def read_wayback_release_cache(cache_path: Path) -> WaybackReleaseCachePayload | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != WAYBACK_RELEASES_CACHE_SCHEMA_VERSION:
            return None
        fetched_at_raw = payload.get("fetched_at")
        source_url = payload.get("source_url")
        releases_raw = payload.get("releases")
        if not isinstance(fetched_at_raw, str) or not isinstance(source_url, str) or not isinstance(releases_raw, list):
            raise ValueError("Wayback releases cache payload is malformed.")
        releases = [_deserialize_release(item) for item in releases_raw if isinstance(item, dict)]
        fetched_at = datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00")).astimezone(UTC)
        return WaybackReleaseCachePayload(
            fetched_at=fetched_at,
            source_url=source_url,
            releases=releases,
        )
    except Exception:
        cache_path.unlink(missing_ok=True)
        return None


def write_wayback_release_cache_atomic(cache_path: Path, payload: dict[str, object]) -> Path:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(cache_path)
    return cache_path
