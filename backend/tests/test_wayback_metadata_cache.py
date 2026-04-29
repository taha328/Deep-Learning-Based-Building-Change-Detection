from __future__ import annotations

import json
import zipfile
from datetime import date, datetime, timedelta, timezone

from src.config import Settings
from src.domain.artifact_manifest import build_manifest, iter_exportable_artifacts, write_manifest_atomic
from src.domain.exports import create_export_bundle_from_manifest
from src.domain.stage_timing import StageTimingRecorder
from src.domain.wayback import MetadataSummary, WaybackRelease
from src.domain.wayback_metadata_cache import (
    build_wayback_metadata_cache_key,
    build_wayback_metadata_cache_payload,
    get_wayback_metadata_cache_path,
    read_wayback_metadata_cache,
    write_wayback_metadata_cache_atomic,
)
from src.services.processing import _resolve_release_for_aoi


class _DummySession:
    def close(self) -> None:
        return None


def _settings(tmp_path, *, enabled: bool = True, ttl_seconds: int = 604800) -> Settings:
    return Settings(
        runtime_cache_dir=tmp_path,
        zoom=19,
        min_zoom=19,
        metadata_grid_size=1,
        wayback_metadata_cache_enabled=enabled,
        wayback_metadata_cache_ttl_seconds=ttl_seconds,
        wayback_tilemap_preflight_enabled=False,
    )


def _release(identifier: str = "WB_2024_R02") -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2024, 3, 7),
        label=f"2024-03-07 | {identifier}",
        release_num=1,
        tile_matrix_sets=("default028mm",),
        resource_url_template=f"https://example.com/{identifier}",
    )


def _summary() -> MetadataSummary:
    return MetadataSummary(
        dominant_src_date="2024-03-07",
        dominant_src_res_m=0.3,
        metadata_region_count=1,
        capture_date_count=1,
        mixed_capture_dates=False,
        metadata_coverage_fraction=0.75,
    )


def _aoi_geojson() -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [[
            [-7.0, 33.0],
            [-6.999, 33.0],
            [-6.999, 33.001],
            [-7.0, 33.001],
            [-7.0, 33.0],
        ]],
    }


def test_wayback_metadata_cache_key_changes_with_release_zoom_and_aoi(tmp_path) -> None:
    settings = _settings(tmp_path)
    bbox = {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001}
    aoi = _aoi_geojson()

    key_1 = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson=aoi,
        zoom=19,
    )
    key_2 = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson=aoi,
        zoom=19,
    )
    key_3 = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2026_R03",
        release_date="2026-03-25",
        bbox=bbox,
        aoi_geojson=aoi,
        zoom=19,
    )
    key_4 = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson=aoi,
        zoom=18,
    )
    key_5 = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson={**aoi, "coordinates": [[[-7.0, 33.0], [-6.999, 33.0], [-6.999, 33.0005], [-7.0, 33.0005], [-7.0, 33.0]]]},
        zoom=19,
    )

    assert key_1 == key_2
    assert key_1 != key_3
    assert key_1 != key_4
    assert key_1 != key_5


def test_wayback_metadata_cache_round_trip_and_expiry(tmp_path) -> None:
    settings = _settings(tmp_path)
    bbox = {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001}
    cache_key = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson=_aoi_geojson(),
        zoom=19,
    )
    cache_path = get_wayback_metadata_cache_path(settings, cache_key)
    payload = build_wayback_metadata_cache_payload(
        settings=settings,
        cache_key=cache_key,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson=_aoi_geojson(),
        zoom=19,
        summary=_summary(),
        ttl_seconds=60,
    )
    write_wayback_metadata_cache_atomic(cache_path, payload)
    assert cache_path.exists()
    assert not any(path.name.startswith(f"{cache_path.name}.tmp-") for path in cache_path.parent.iterdir())

    loaded = read_wayback_metadata_cache(cache_path, cache_key=cache_key, ttl_seconds=60)
    assert loaded == _summary()

    expired_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    expired_payload["created_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    expired_payload["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    cache_path.write_text(json.dumps(expired_payload, indent=2, sort_keys=True), encoding="utf-8")
    assert read_wayback_metadata_cache(cache_path, cache_key=cache_key, ttl_seconds=60) is None
    assert not cache_path.exists()


def test_wayback_metadata_cache_rejects_invalid_json(tmp_path) -> None:
    settings = _settings(tmp_path)
    bbox = {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001}
    cache_key = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox=bbox,
        aoi_geojson=_aoi_geojson(),
        zoom=19,
    )
    cache_path = get_wayback_metadata_cache_path(settings, cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not-json", encoding="utf-8")

    assert read_wayback_metadata_cache(cache_path, cache_key=cache_key, ttl_seconds=60) is None
    assert not cache_path.exists()


def test_release_resolution_uses_wayback_metadata_cache_on_second_lookup(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    release = _release()
    recorder_first = StageTimingRecorder(run_id="run-cache-1", pipeline_kind="detection")
    recorder_second = StageTimingRecorder(run_id="run-cache-2", pipeline_kind="detection")
    calls: list[int] = []

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())

    def fake_summarize(session, release_identifier, bbox, *, grid_size, aoi_geojson=None, zoom=19):
        del session, release_identifier, bbox, grid_size, aoi_geojson
        calls.append(zoom)
        return _summary()

    monkeypatch.setattr("src.services.processing.summarize_wayback_metadata", fake_summarize)

    first = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox={"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001},
        normalized_aoi=_aoi_geojson(),
        timing=recorder_first,
        stage_prefix="release_resolution",
        scene_role="single",
    )
    second = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox={"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001},
        normalized_aoi=_aoi_geojson(),
        timing=recorder_second,
        stage_prefix="release_resolution",
        scene_role="single",
    )

    assert calls == [19]
    assert first.zoom == 19
    assert second.zoom == 19
    assert first.metadata == second.metadata

    first_payload = recorder_first.to_dict()
    second_payload = recorder_second.to_dict()
    first_metadata_lookup = next(stage for stage in first_payload["stages"] if stage["name"] == "release_resolution.metadata_lookup")
    second_metadata_lookup = next(stage for stage in second_payload["stages"] if stage["name"] == "release_resolution.metadata_lookup")

    assert first_metadata_lookup["metadata"]["cache_enabled"] is True
    assert first_metadata_lookup["metadata"]["cache_hit"] is False
    assert first_metadata_lookup["metadata"]["cache_key"].startswith("wayback-metadata-")
    assert first_metadata_lookup["metadata"]["cache_path_exists"] is False

    assert second_metadata_lookup["metadata"]["cache_enabled"] is True
    assert second_metadata_lookup["metadata"]["cache_hit"] is True
    assert second_metadata_lookup["metadata"]["cache_key"] == first_metadata_lookup["metadata"]["cache_key"]
    assert second_metadata_lookup["metadata"]["cache_path_exists"] is True
    assert "release_resolution.session_setup" not in [stage["name"] for stage in second_payload["stages"]]


def test_wayback_metadata_cache_files_do_not_enter_request_exports(tmp_path) -> None:
    settings = _settings(tmp_path)
    request_dir = settings.request_cache_dir / "run-export"
    request_dir.mkdir(parents=True, exist_ok=True)
    final_path = request_dir / "building_change_blocks.geojson"
    final_path.write_text("{\"type\":\"FeatureCollection\",\"features\":[]}", encoding="utf-8")
    manifest = build_manifest("run-export", request_dir, [{"path": str(final_path)}])
    write_manifest_atomic(request_dir, manifest)

    cache_key = build_wayback_metadata_cache_key(
        settings,
        release_identifier="WB_2024_R02",
        release_date="2024-03-07",
        bbox={"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001},
        aoi_geojson=_aoi_geojson(),
        zoom=19,
    )
    cache_path = get_wayback_metadata_cache_path(settings, cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{}", encoding="utf-8")

    exportable = iter_exportable_artifacts(request_dir)
    assert cache_path not in exportable

    bundle_path = create_export_bundle_from_manifest(request_dir, force=True)
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())

    assert "building_change_blocks.geojson" in names
    assert cache_path.name not in names
