from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
import requests

from src.config import Settings
from src.domain.wayback import WaybackRelease
from src.domain.wayback_release_cache import (
    build_wayback_release_cache_payload,
    read_wayback_release_cache,
)
from src.services import releases as releases_service


@pytest.fixture(autouse=True)
def _clear_releases_memory_cache() -> None:
    releases_service._memory_cache.clear()


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(runtime_cache_dir=tmp_path, **overrides)


def _sample_release(identifier: str = "WB_2026_R04") -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2026, 3, 25),
        label=f"2026-03-25 | {identifier}",
        release_num=4,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://example.com/tile/22869/{TileMatrix}/{TileRow}/{TileCol}",
    )


def test_wayback_releases_live_fetch_updates_disk_cache(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        releases_service,
        "parse_wmts_capabilities",
        lambda session, url: [_sample_release()],
    )

    snapshot = releases_service.resolve_releases_snapshot(settings)

    assert snapshot.source_status == "live"
    cached = read_wayback_release_cache(settings.wayback_releases_cache_path)
    assert cached is not None
    assert cached.releases[0].identifier == "WB_2026_R04"


def test_wayback_releases_returns_memory_cache_within_ttl(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, wayback_releases_cache_ttl_seconds=86400)
    call_count = {"value": 0}

    def _fake_parse(_session, _url):
        call_count["value"] += 1
        return [_sample_release()]

    monkeypatch.setattr(releases_service, "parse_wmts_capabilities", _fake_parse)

    first = releases_service.resolve_releases_snapshot(settings)
    second = releases_service.resolve_releases_snapshot(settings)

    assert first.releases[0].identifier == second.releases[0].identifier
    assert call_count["value"] == 1


def test_wayback_releases_returns_disk_cache_when_live_dns_fails(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    payload = build_wayback_release_cache_payload(
        source_url=settings.wmts_capabilities_url,
        releases=[_sample_release()],
        fetched_at=datetime.now(UTC) - timedelta(days=2),
    )
    settings.wayback_releases_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.wayback_releases_cache_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        releases_service,
        "parse_wmts_capabilities",
        lambda _session, _url: (_ for _ in ()).throw(requests.exceptions.ConnectionError("dns failed")),
    )

    snapshot = releases_service.resolve_releases_snapshot(settings)

    assert snapshot.source_status == "stale"
    assert snapshot.warnings[0]["code"] == "wayback_releases_stale_fallback"


def test_wayback_releases_returns_disk_cache_when_live_timeout_fails(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    payload = build_wayback_release_cache_payload(
        source_url=settings.wmts_capabilities_url,
        releases=[_sample_release()],
        fetched_at=datetime.now(UTC) - timedelta(days=5),
    )
    settings.wayback_releases_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.wayback_releases_cache_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        releases_service,
        "parse_wmts_capabilities",
        lambda _session, _url: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("timeout")),
    )

    snapshot = releases_service.resolve_releases_snapshot(settings)

    assert snapshot.source_status == "stale"
    assert snapshot.releases[0].identifier == "WB_2026_R04"


def test_wayback_releases_corrupt_disk_cache_is_ignored_or_cleaned(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.wayback_releases_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.wayback_releases_cache_path.write_text("{not-json", encoding="utf-8")

    cached = read_wayback_release_cache(settings.wayback_releases_cache_path)

    assert cached is None
    assert not settings.wayback_releases_cache_path.exists()


def test_wayback_releases_expired_cache_can_be_used_when_stale_if_error_enabled(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, wayback_releases_cache_ttl_seconds=1, wayback_releases_stale_if_error_enabled=True)
    payload = build_wayback_release_cache_payload(
        source_url=settings.wmts_capabilities_url,
        releases=[_sample_release()],
        fetched_at=datetime.now(UTC) - timedelta(days=3),
    )
    settings.wayback_releases_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.wayback_releases_cache_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        releases_service,
        "parse_wmts_capabilities",
        lambda _session, _url: (_ for _ in ()).throw(requests.exceptions.ConnectionError("offline")),
    )

    snapshot = releases_service.resolve_releases_snapshot(settings)

    assert snapshot.source_status == "stale"


def test_wayback_releases_no_cache_live_failure_returns_controlled_503(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        releases_service,
        "parse_wmts_capabilities",
        lambda _session, _url: (_ for _ in ()).throw(requests.exceptions.ConnectionError("offline")),
    )

    with pytest.raises(releases_service.ReleaseServiceError) as exc_info:
        releases_service.resolve_releases_snapshot(settings)

    assert exc_info.value.code == "wayback_releases_unreachable"
    assert exc_info.value.details["cache_available"] is False
