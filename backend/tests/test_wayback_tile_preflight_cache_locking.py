from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from src.config import Settings
from src.domain.wayback import TileAvailabilitySummary, WaybackRelease
from src.domain.wayback_tile_preflight_cache import (
    WaybackTilePreflightCacheLockTimeout,
    build_wayback_tile_preflight_cache_key,
    build_wayback_tile_preflight_cache_payload,
    cleanup_wayback_preflight_locks,
    cleanup_stale_wayback_tile_preflight_locks,
    get_wayback_tile_preflight_cache_path,
    wayback_tile_preflight_cache_lock_path,
    write_wayback_tile_preflight_cache_atomic,
)
from src.services.processing import _preflight_release_tile_availability_for_request


class _DummySession:
    def close(self) -> None:
        return None


def _settings(
    tmp_path,
    *,
    cache_enabled: bool = True,
    metadata_workers: int = 7,
    preflight_workers: int | None = None,
    adaptive_enabled: bool = True,
    adaptive_initial: int = 10,
    adaptive_min: int = 4,
    adaptive_step: int = 2,
) -> Settings:
    return Settings(
        runtime_cache_dir=tmp_path,
        zoom=19,
        min_zoom=19,
        metadata_grid_size=1,
        wayback_metadata_workers=metadata_workers,
        wayback_metadata_workers_adaptive_enabled=adaptive_enabled,
        wayback_metadata_workers_initial=adaptive_initial,
        wayback_metadata_workers_min=adaptive_min,
        wayback_metadata_workers_step=adaptive_step,
        wayback_tilemap_preflight_workers=preflight_workers,
        wayback_tile_preflight_cache_enabled=cache_enabled,
        wayback_tilemap_preflight_enabled=True,
    )


def _release(identifier: str = "WB_2024_R02") -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2024, 3, 7),
        label=f"2024-03-07 | {identifier}",
        release_num=1,
        tile_matrix_sets=("default028mm",),
        resource_url_template=f"https://example.com/{identifier}/tile/1" + "/{TileMatrix}/{TileRow}/{TileCol}",
    )


def _bbox() -> dict[str, float]:
    return {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001}


def _tilemap(*, available_count: int = 1) -> TileAvailabilitySummary:
    return TileAvailabilitySummary(
        candidate_count=2,
        available_count=available_count,
        missing_count=max(2 - available_count, 0),
        failed_check_count=0,
        preflight_complete=True,
        availability_fraction=available_count / 2,
        available_tiles=frozenset({(0, 0)}) if available_count else frozenset(),
    )


def _cache_path(settings: Settings, release: WaybackRelease):
    cache_key = build_wayback_tile_preflight_cache_key(
        settings,
        release=release,
        bbox=_bbox(),
        aoi_geojson=None,
        zoom=19,
    )
    return cache_key, get_wayback_tile_preflight_cache_path(settings, cache_key)


def _write_valid_cache(settings: Settings, release: WaybackRelease, summary: TileAvailabilitySummary) -> None:
    cache_key, cache_path = _cache_path(settings, release)
    payload = build_wayback_tile_preflight_cache_payload(
        settings=settings,
        cache_key=cache_key,
        release=release,
        bbox=_bbox(),
        aoi_geojson=None,
        zoom=19,
        tilemap=summary,
        ttl_seconds=settings.wayback_tile_preflight_cache_ttl_seconds,
    )
    write_wayback_tile_preflight_cache_atomic(cache_path, payload)


def test_live_preflight_runs_outside_cache_lock(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    release = _release()
    lock_held = False

    @contextmanager
    def fake_lock(cache_path):
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def fake_preflight(session, release_value, bbox, *, zoom, max_workers, **kwargs):
        del kwargs
        del session, release_value, bbox, zoom, max_workers
        assert lock_held is False
        return _tilemap()

    monkeypatch.setattr("src.services.processing.acquire_wayback_tile_preflight_cache_lock", fake_lock)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    summary = _preflight_release_tile_availability_for_request(settings, release=release, aoi_bbox=_bbox(), zoom=19)

    assert summary == _tilemap()


def test_rechecks_cache_before_write_after_live_preflight(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    release = _release()
    race_summary = _tilemap(available_count=2)
    read_calls = 0
    write_calls = 0

    def fake_read(*args, **kwargs):
        nonlocal read_calls
        read_calls += 1
        return None if read_calls == 1 else race_summary

    def fake_write(*args, **kwargs):
        nonlocal write_calls
        write_calls += 1

    monkeypatch.setattr("src.services.processing.read_wayback_tile_preflight_cache", fake_read)
    monkeypatch.setattr("src.services.processing.write_wayback_tile_preflight_cache_atomic", fake_write)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr(
        "src.services.processing.preflight_wayback_tile_availability",
        lambda *args, **kwargs: _tilemap(available_count=1),
    )

    summary = _preflight_release_tile_availability_for_request(settings, release=release, aoi_bbox=_bbox(), zoom=19)

    assert summary == race_summary
    assert read_calls == 2
    assert write_calls == 0


def test_lock_timeout_uses_cache_if_json_appeared(tmp_path, monkeypatch, caplog) -> None:
    settings = _settings(tmp_path)
    release = _release()
    cached_summary = _tilemap(available_count=2)
    _write_valid_cache(settings, release, cached_summary)
    _, cache_path = _cache_path(settings, release)

    def timeout_lock(cache_path):
        raise WaybackTilePreflightCacheLockTimeout(wayback_tile_preflight_cache_lock_path(cache_path))

    monkeypatch.setattr("src.services.processing.acquire_wayback_tile_preflight_cache_lock", timeout_lock)
    monkeypatch.setattr(
        "src.services.processing.preflight_wayback_tile_availability",
        lambda *args, **kwargs: pytest.fail("live preflight should not run when cache appears after timeout"),
    )

    caplog.set_level("INFO")
    summary = _preflight_release_tile_availability_for_request(settings, release=release, aoi_bbox=_bbox(), zoom=19)

    assert summary == cached_summary
    assert cache_path.exists()
    assert "PREFLIGHT_LOCK_TIMEOUT_CACHE_APPEARED" in caplog.text


def test_lock_timeout_logs_and_falls_back_without_deleting_lock(tmp_path, monkeypatch, caplog) -> None:
    settings = _settings(tmp_path)
    release = _release()
    _, cache_path = _cache_path(settings, release)
    lock_path = wayback_tile_preflight_cache_lock_path(cache_path)
    lock_path.mkdir()

    def timeout_lock(cache_path):
        raise WaybackTilePreflightCacheLockTimeout(wayback_tile_preflight_cache_lock_path(cache_path))

    monkeypatch.setattr("src.services.processing.acquire_wayback_tile_preflight_cache_lock", timeout_lock)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr(
        "src.services.processing.preflight_wayback_tile_availability",
        lambda *args, **kwargs: _tilemap(available_count=1),
    )

    caplog.set_level("WARNING")
    summary = _preflight_release_tile_availability_for_request(settings, release=release, aoi_bbox=_bbox(), zoom=19)

    assert summary == _tilemap(available_count=1)
    assert lock_path.exists()
    assert "PREFLIGHT_CACHE_LOCK_TIMEOUT" in caplog.text


def test_stale_cleanup_deletes_only_old_safe_lock(tmp_path) -> None:
    cache_dir = tmp_path / "preflight"
    cache_dir.mkdir()
    stale_lock = cache_dir / "stale.json.lock"
    stale_lock.mkdir()
    preserved_cache = cache_dir / "preserved.json"
    preserved_cache.write_text("{}", encoding="utf-8")
    blocked_lock = cache_dir / "preserved.json.lock"
    blocked_lock.mkdir()
    old_time = time.time() - 7200
    os.utime(stale_lock, (old_time, old_time))
    os.utime(blocked_lock, (old_time, old_time))

    result = cleanup_stale_wayback_tile_preflight_locks(cache_dir, stale_seconds=3600)

    assert stale_lock not in result.skipped
    assert not stale_lock.exists()
    assert blocked_lock.exists()
    assert preserved_cache.exists()
    assert result.deleted_paths == (stale_lock.resolve(),)


def test_stale_cleanup_skips_fresh_lock(tmp_path) -> None:
    cache_dir = tmp_path / "preflight"
    cache_dir.mkdir()
    fresh_lock = cache_dir / "fresh.json.lock"
    fresh_lock.mkdir()

    result = cleanup_stale_wayback_tile_preflight_locks(cache_dir, stale_seconds=3600)

    assert fresh_lock.exists()
    assert result.deleted_paths == ()
    assert result.skipped[0]["reason"] == "fresh"


def test_stale_cleanup_rejects_lock_outside_cache_path(tmp_path) -> None:
    cache_dir = tmp_path / "preflight"
    outside_dir = tmp_path / "outside-lock"
    cache_dir.mkdir()
    outside_dir.mkdir()
    link_path = cache_dir / "outside.json.lock"
    try:
        link_path.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    result = cleanup_stale_wayback_tile_preflight_locks(cache_dir, stale_seconds=1)

    assert link_path.exists()
    assert result.deleted_paths == ()
    assert result.skipped[0]["reason"] == "outside_cache_dir"


def test_startup_cleanup_deletes_lock_paths_nested_and_preserves_non_locks(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    cache_dir = settings.wayback_tile_preflight_cache_dir
    top_lock_file = cache_dir / "top.lock"
    top_lock_file.write_text("locked", encoding="utf-8")
    top_lock_dir = cache_dir / "top.json.lock"
    top_lock_dir.mkdir()
    (top_lock_dir / "owner.pid").write_text("999999", encoding="utf-8")
    nested_dir = cache_dir / "nested" / "deeper"
    nested_dir.mkdir(parents=True)
    nested_lock = nested_dir / "nested.lock"
    nested_lock.write_text("locked", encoding="utf-8")
    preserved_cache = cache_dir / "top.json"
    preserved_cache.write_text("{}", encoding="utf-8")
    preserved_almost_lock = cache_dir / "top.lock.json"
    preserved_almost_lock.write_text("{}", encoding="utf-8")

    result = cleanup_wayback_preflight_locks("unit_test", settings=settings, source="pytest")

    assert not top_lock_file.exists()
    assert not top_lock_dir.exists()
    assert not nested_lock.exists()
    assert preserved_cache.exists()
    assert preserved_almost_lock.exists()
    assert result["deleted_count"] == 3
    assert result["failed_count"] == 0
    assert result["cache_dir"] == str(cache_dir.resolve())
    assert result["trigger"] == "unit_test"
    assert result["source"] == "pytest"


def test_startup_cleanup_missing_cache_dir_logs_skip_and_continues(tmp_path, caplog) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    cache_dir = settings.wayback_tile_preflight_cache_dir
    shutil.rmtree(cache_dir)

    with caplog.at_level("INFO"):
        result = cleanup_wayback_preflight_locks("unit_test_missing", settings=settings, source="pytest")

    assert result["deleted_count"] == 0
    assert result["failed_count"] == 0
    assert result["skipped"] is True
    assert result["skip_reason"] == "cache_dir_missing"
    assert "WAYBACK_PREFLIGHT_LOCK_CLEANUP_START" in caplog.text
    assert "WAYBACK_PREFLIGHT_LOCK_CLEANUP_DONE" in caplog.text
    assert "reason=cache_dir_missing" in caplog.text


def test_startup_cleanup_is_idempotent(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    lock_path = settings.wayback_tile_preflight_cache_dir / "once.lock"
    lock_path.write_text("locked", encoding="utf-8")

    first = cleanup_wayback_preflight_locks("unit_test_idempotent", settings=settings, source="pytest")
    second = cleanup_wayback_preflight_locks("unit_test_idempotent", settings=settings, source="pytest")

    assert first["deleted_count"] == 1
    assert first["failed_count"] == 0
    assert second["deleted_count"] == 0
    assert second["failed_count"] == 0
    assert not lock_path.exists()


def test_startup_cleanup_does_not_delete_outside_cache_path(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    cache_dir = settings.wayback_tile_preflight_cache_dir
    outside_lock = tmp_path / "outside.lock"
    outside_lock.write_text("outside", encoding="utf-8")
    inside_link = cache_dir / "outside-link.lock"
    try:
        inside_link.symlink_to(outside_lock)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    result = cleanup_wayback_preflight_locks("unit_test_outside", settings=settings, source="pytest")

    assert outside_lock.exists()
    assert inside_link.exists()
    assert result["deleted_count"] == 0
    assert result["failed_count"] == 1
    assert result["failed_paths"][0]["reason"] == "outside_cache_dir"


def test_startup_cleanup_warns_and_continues_on_delete_race(tmp_path, monkeypatch, caplog) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    race_lock = settings.wayback_tile_preflight_cache_dir / "race.lock"
    other_lock = settings.wayback_tile_preflight_cache_dir / "other.lock"
    race_lock.write_text("locked", encoding="utf-8")
    other_lock.write_text("locked", encoding="utf-8")
    original_unlink = Path.unlink

    def race_unlink(self, *args, **kwargs):
        if self == race_lock:
            raise FileNotFoundError("race removed by another worker")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", race_unlink)

    with caplog.at_level("WARNING"):
        result = cleanup_wayback_preflight_locks("unit_test_race", settings=settings, source="pytest")

    assert race_lock.exists()
    assert not other_lock.exists()
    assert result["deleted_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed_paths"][0]["reason"] == "lock_path_delete_failed"
    assert "WAYBACK_PREFLIGHT_LOCK_CLEANUP_WARNING" in caplog.text


def test_adaptive_preflight_workers_start_at_initial_workers(tmp_path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        cache_enabled=False,
        metadata_workers=7,
        adaptive_initial=10,
        adaptive_min=4,
        adaptive_step=2,
    )
    captured_workers: list[int] = []
    captured_adaptive: list[dict[str, object]] = []

    def fake_preflight(
        session,
        release_value,
        bbox,
        *,
        zoom,
        max_workers,
        adaptive_enabled,
        adaptive_min_workers,
        adaptive_step,
    ):
        del session, release_value, bbox, zoom
        captured_workers.append(max_workers)
        captured_adaptive.append(
            {
                "adaptive_enabled": adaptive_enabled,
                "adaptive_min_workers": adaptive_min_workers,
                "adaptive_step": adaptive_step,
            }
        )
        return _tilemap()

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    _preflight_release_tile_availability_for_request(settings, release=_release(), aoi_bbox=_bbox(), zoom=19)

    assert captured_workers == [10]
    assert captured_adaptive == [
        {
            "adaptive_enabled": True,
            "adaptive_min_workers": 4,
            "adaptive_step": 2,
        }
    ]


def test_fixed_preflight_workers_default_to_metadata_workers_when_adaptive_disabled(tmp_path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        cache_enabled=False,
        metadata_workers=7,
        preflight_workers=None,
        adaptive_enabled=False,
    )
    captured_workers: list[int] = []
    captured_adaptive: list[bool] = []

    def fake_preflight(session, release_value, bbox, *, zoom, max_workers, adaptive_enabled, **kwargs):
        del session, release_value, bbox, zoom, kwargs
        captured_workers.append(max_workers)
        captured_adaptive.append(adaptive_enabled)
        return _tilemap()

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    _preflight_release_tile_availability_for_request(settings, release=_release(), aoi_bbox=_bbox(), zoom=19)

    assert captured_workers == [7]
    assert captured_adaptive == [False]


def test_explicit_preflight_workers_override_metadata_workers(tmp_path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        cache_enabled=False,
        metadata_workers=7,
        preflight_workers=3,
        adaptive_enabled=False,
    )
    captured_workers: list[int] = []

    def fake_preflight(session, release_value, bbox, *, zoom, max_workers, **kwargs):
        del kwargs
        del session, release_value, bbox, zoom
        captured_workers.append(max_workers)
        return _tilemap()

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    _preflight_release_tile_availability_for_request(settings, release=_release(), aoi_bbox=_bbox(), zoom=19)

    assert captured_workers == [3]


def test_adaptive_preflight_logs_final_worker_count(tmp_path, monkeypatch, caplog) -> None:
    settings = _settings(tmp_path, cache_enabled=False, adaptive_initial=10, adaptive_min=4, adaptive_step=2)

    def fake_preflight(session, release_value, bbox, *, zoom, max_workers, **kwargs):
        del release_value, bbox, zoom, max_workers, kwargs
        session.wayback_preflight_final_workers = 8
        session.wayback_preflight_downshift_count = 1
        return _tilemap()

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    caplog.set_level("INFO")
    _preflight_release_tile_availability_for_request(settings, release=_release(), aoi_bbox=_bbox(), zoom=19)

    assert "PREFLIGHT_ADAPTIVE_POLICY" in caplog.text
    assert "source=adaptive_initial" in caplog.text
    assert "finalWorkers=8 downshiftCount=1" in caplog.text


def test_adaptive_worker_config_clamps_invalid_values(tmp_path, caplog) -> None:
    caplog.set_level("WARNING")

    settings = Settings(
        runtime_cache_dir=tmp_path,
        wayback_metadata_workers_initial=2,
        wayback_metadata_workers_min=4,
        wayback_metadata_workers_step=0,
    )

    assert settings.wayback_metadata_workers_initial == 4
    assert settings.wayback_metadata_workers_min == 4
    assert settings.wayback_metadata_workers_step == 1
    assert "WAYBACK_METADATA_WORKERS_ADAPTIVE_CONFIG_CLAMPED" in caplog.text
