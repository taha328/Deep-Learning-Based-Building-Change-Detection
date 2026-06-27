from __future__ import annotations

import json
import zipfile
from datetime import date, datetime, timedelta, timezone

from src.config import Settings
from src.domain.artifact_manifest import build_manifest, iter_exportable_artifacts, write_manifest_atomic
from src.domain.exports import create_export_bundle_from_manifest
from src.domain.wayback import MetadataSummary, TileAvailabilitySummary, WaybackRelease
from src.domain.wayback_tile_preflight_cache import (
    build_wayback_tile_preflight_cache_key,
    build_wayback_tile_preflight_cache_payload,
    get_wayback_tile_preflight_cache_path,
    read_wayback_tile_preflight_cache,
    write_wayback_tile_preflight_cache_atomic,
)
from src.services.processing import _resolve_release_for_aoi
from src.domain.stage_timing import StageTimingRecorder


def _settings(tmp_path, *, enabled: bool = True, ttl_seconds: int = 604800) -> Settings:
    return Settings(
        runtime_cache_dir=tmp_path,
        zoom=19,
        min_zoom=19,
        metadata_grid_size=1,
        wayback_tile_preflight_cache_enabled=enabled,
        wayback_tile_preflight_cache_ttl_seconds=ttl_seconds,
        wayback_tilemap_preflight_enabled=True,
    )


def _release(identifier: str = "WB_2024_R02", *, release_num: int = 1) -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2024, 3, 7),
        label=f"2024-03-07 | {identifier}",
        release_num=release_num,
        tile_matrix_sets=("default028mm",),
        resource_url_template=f"https://example.com/{identifier}/tile/{release_num}" + "/{TileMatrix}/{TileRow}/{TileCol}",
    )


def _aoi_bbox() -> dict[str, float]:
    return {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001}


def _tilemap_summary(*, available_count: int, failed_check_count: int = 0) -> TileAvailabilitySummary:
    return TileAvailabilitySummary(
        candidate_count=2,
        available_count=available_count,
        missing_count=max(2 - available_count - failed_check_count, 0),
        failed_check_count=failed_check_count,
        preflight_complete=failed_check_count == 0,
        availability_fraction=available_count / 2,
        available_tiles=frozenset({(0, 0)}) if available_count else frozenset(),
    )


def test_wayback_tile_preflight_cache_key_changes_with_release_zoom_and_aoi(tmp_path) -> None:
    settings = _settings(tmp_path)
    bbox = _aoi_bbox()

    key_1 = build_wayback_tile_preflight_cache_key(settings, release=_release(), bbox=bbox, aoi_geojson=None, zoom=19)
    key_2 = build_wayback_tile_preflight_cache_key(settings, release=_release(), bbox=bbox, aoi_geojson=None, zoom=19)
    key_3 = build_wayback_tile_preflight_cache_key(
        settings,
        release=_release("WB_2026_R03"),
        bbox=bbox,
        aoi_geojson=None,
        zoom=19,
    )
    key_4 = build_wayback_tile_preflight_cache_key(settings, release=_release(), bbox=bbox, aoi_geojson=None, zoom=18)
    key_5 = build_wayback_tile_preflight_cache_key(
        settings,
        release=_release(),
        bbox={"west": -7.0, "south": 33.0, "east": -6.998, "north": 33.001},
        aoi_geojson=None,
        zoom=19,
    )

    assert key_1 == key_2
    assert key_1 != key_3
    assert key_1 != key_4
    assert key_1 != key_5


def test_wayback_tile_preflight_cache_round_trip_and_expiry(tmp_path) -> None:
    settings = _settings(tmp_path)
    cache_key = build_wayback_tile_preflight_cache_key(
        settings,
        release=_release(),
        bbox=_aoi_bbox(),
        aoi_geojson=None,
        zoom=19,
    )
    cache_path = get_wayback_tile_preflight_cache_path(settings, cache_key)
    payload = build_wayback_tile_preflight_cache_payload(
        settings=settings,
        cache_key=cache_key,
        release=_release(),
        bbox=_aoi_bbox(),
        aoi_geojson=None,
        zoom=19,
        tilemap=_tilemap_summary(available_count=1),
        ttl_seconds=60,
    )
    write_wayback_tile_preflight_cache_atomic(cache_path, payload)
    assert cache_path.exists()
    assert not any(path.name.startswith(f"{cache_path.name}.tmp-") for path in cache_path.parent.iterdir())

    loaded = read_wayback_tile_preflight_cache(cache_path, cache_key=cache_key, ttl_seconds=60)
    assert loaded == _tilemap_summary(available_count=1)

    expired_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    expired_payload["created_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    expired_payload["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    cache_path.write_text(json.dumps(expired_payload, indent=2, sort_keys=True), encoding="utf-8")

    assert read_wayback_tile_preflight_cache(cache_path, cache_key=cache_key, ttl_seconds=60) is None
    assert not cache_path.exists()


def test_wayback_tile_preflight_cache_rejects_invalid_json(tmp_path) -> None:
    settings = _settings(tmp_path)
    cache_key = build_wayback_tile_preflight_cache_key(
        settings,
        release=_release(),
        bbox=_aoi_bbox(),
        aoi_geojson=None,
        zoom=19,
    )
    cache_path = get_wayback_tile_preflight_cache_path(settings, cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not-json", encoding="utf-8")

    assert read_wayback_tile_preflight_cache(cache_path, cache_key=cache_key, ttl_seconds=60) is None
    assert not cache_path.exists()


def test_release_resolution_uses_tile_preflight_cache_on_second_lookup(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    release = _release()
    recorder_first = StageTimingRecorder(run_id="run-tile-cache-1", pipeline_kind="detection")
    recorder_second = StageTimingRecorder(run_id="run-tile-cache-2", pipeline_kind="detection")
    metadata_summary = MetadataSummary(
        dominant_src_date="2024-03-07",
        dominant_src_res_m=0.3,
        metadata_region_count=1,
        capture_date_count=1,
        mixed_capture_dates=False,
        metadata_coverage_fraction=0.75,
    )
    calls: list[int] = []

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr("src.services.processing.summarize_wayback_metadata", lambda *args, **kwargs: metadata_summary)

    def fake_preflight(session, release_value, bbox, *, zoom, max_workers, **kwargs):
        del kwargs
        del session, release_value, bbox, max_workers
        calls.append(zoom)
        return _tilemap_summary(available_count=1)

    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    first = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox=_aoi_bbox(),
        normalized_aoi={"type": "Polygon", "coordinates": []},
        timing=recorder_first,
        stage_prefix="release_resolution",
        scene_role="single",
    )
    second = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox=_aoi_bbox(),
        normalized_aoi={"type": "Polygon", "coordinates": []},
        timing=recorder_second,
        stage_prefix="release_resolution",
        scene_role="single",
    )

    assert calls == [19]
    assert first.zoom == 19
    assert second.zoom == 19

    first_payload = recorder_first.to_dict()
    second_payload = recorder_second.to_dict()
    first_stage = next(
        stage for stage in first_payload["stages"] if stage["name"] == "release_resolution.tile_availability_preflight"
    )
    second_stage = next(
        stage for stage in second_payload["stages"] if stage["name"] == "release_resolution.tile_availability_preflight"
    )

    assert first_stage["metadata"]["cache_enabled"] is True
    assert first_stage["metadata"]["cache_hit"] is False
    assert first_stage["metadata"]["cache_key"].startswith("wayback-tile-preflight-")
    assert first_stage["metadata"]["cache_path_exists"] is False

    assert second_stage["metadata"]["cache_enabled"] is True
    assert second_stage["metadata"]["cache_hit"] is True
    assert second_stage["metadata"]["cache_key"] == first_stage["metadata"]["cache_key"]
    assert second_stage["metadata"]["cache_path_exists"] is True
    assert "release_resolution.session_setup" not in [stage["name"] for stage in second_payload["stages"]]


def test_wayback_tile_preflight_cache_files_do_not_enter_request_exports(tmp_path) -> None:
    settings = _settings(tmp_path)
    request_dir = settings.request_cache_dir / "run-export"
    request_dir.mkdir(parents=True, exist_ok=True)
    final_path = request_dir / "building_change_blocks.geojson"
    final_path.write_text("{\"type\":\"FeatureCollection\",\"features\":[]}", encoding="utf-8")
    manifest = build_manifest("run-export", request_dir, [{"path": str(final_path)}])
    write_manifest_atomic(request_dir, manifest)

    cache_key = build_wayback_tile_preflight_cache_key(
        settings,
        release=_release(),
        bbox=_aoi_bbox(),
        aoi_geojson=None,
        zoom=19,
    )
    cache_path = get_wayback_tile_preflight_cache_path(settings, cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{}", encoding="utf-8")

    exportable = iter_exportable_artifacts(request_dir)
    assert cache_path not in exportable

    bundle_path = create_export_bundle_from_manifest(request_dir, force=True)
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())

    assert "building_change_blocks.geojson" in names
    assert cache_path.name not in names
