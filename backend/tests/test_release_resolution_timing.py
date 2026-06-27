from __future__ import annotations

from datetime import date

from src.config import Settings
from src.domain.stage_timing import StageTimingRecorder
from src.domain.wayback import MetadataSummary, TileAvailabilitySummary, WaybackRelease
from src.services.processing import _resolve_release_for_aoi


class _DummySession:
    def close(self) -> None:
        return None


def _release(identifier: str = "WB_2024_R02", *, release_num: int = 1) -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2024, 3, 7),
        label=f"2024-03-07 | {identifier}",
        release_num=release_num,
        tile_matrix_sets=("default028mm",),
        resource_url_template=f"https://example.com/{identifier}",
    )


def _settings(tmp_path, *, zoom: int = 19, min_zoom: int = 18, preflight: bool = False) -> Settings:
    return Settings(
        runtime_cache_dir=tmp_path,
        zoom=zoom,
        min_zoom=min_zoom,
        metadata_grid_size=1,
        wayback_metadata_workers=1,
        wayback_tilemap_preflight_enabled=preflight,
    )


def _metadata_summary(*, usable: bool) -> MetadataSummary:
    if usable:
        return MetadataSummary(
            dominant_src_date="2024-03-07",
            dominant_src_res_m=0.3,
            metadata_region_count=1,
            capture_date_count=1,
            mixed_capture_dates=False,
            metadata_coverage_fraction=0.75,
        )
    return MetadataSummary(
        dominant_src_date=None,
        dominant_src_res_m=None,
        metadata_region_count=0,
        capture_date_count=0,
        mixed_capture_dates=False,
        metadata_coverage_fraction=0.0,
    )


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


def test_resolve_release_records_metadata_lookup_and_zoom_attempts(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, zoom=19, min_zoom=18, preflight=False)
    release = _release()
    recorder = StageTimingRecorder(run_id="run-1", pipeline_kind="detection")
    requested_zooms: list[int] = []

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())

    def fake_summarize(session, release_identifier, bbox, *, grid_size, aoi_geojson=None, zoom=19):
        requested_zooms.append(zoom)
        return _metadata_summary(usable=zoom == 18)

    monkeypatch.setattr("src.services.processing.summarize_wayback_metadata", fake_summarize)

    resolved = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox={"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0},
        normalized_aoi={"type": "Polygon", "coordinates": []},
        timing=recorder,
        stage_prefix="release_resolution",
        scene_role="single",
    )

    assert resolved.zoom == 18
    assert requested_zooms == [19, 18]
    payload = recorder.to_dict()
    stage_names = [stage["name"] for stage in payload["stages"]]
    assert "release_resolution.total" in stage_names
    assert stage_names.count("release_resolution.session_setup") == 2
    assert stage_names.count("release_resolution.metadata_lookup") == 2
    assert stage_names.count("release_resolution.zoom_attempt") == 2
    assert "release_resolution.decision" in stage_names
    attempts = [stage for stage in payload["stages"] if stage["name"] == "release_resolution.zoom_attempt"]
    assert [stage["metadata"]["attempt_index"] for stage in attempts] == [1, 2]
    assert [stage["metadata"]["zoom"] for stage in attempts] == [19, 18]
    assert attempts[0]["metadata"]["coverage_ok"] is False
    assert attempts[1]["metadata"]["coverage_ok"] is True
    decision = next(stage for stage in payload["stages"] if stage["name"] == "release_resolution.decision")
    assert decision["metadata"]["selected_zoom"] == 18
    assert decision["metadata"]["fallback_used"] is False
    total = next(stage for stage in payload["stages"] if stage["name"] == "release_resolution.total")
    assert total["metadata"]["attempt_count"] == 2
    assert total["metadata"]["attempted_zooms"] == [19, 18]


def test_resolve_release_records_tile_preflight_when_enabled(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, zoom=19, min_zoom=18, preflight=True)
    release = _release()
    recorder = StageTimingRecorder(run_id="run-2", pipeline_kind="detection")

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: _metadata_summary(usable=kwargs["zoom"] == 18),
    )
    monkeypatch.setattr(
        "src.services.processing.preflight_wayback_tile_availability",
        lambda session, release, bbox, *, zoom, max_workers, **kwargs: _tilemap_summary(
            available_count=1 if zoom == 18 else 0,
            failed_check_count=0 if zoom == 18 else 1,
        ),
    )

    resolved = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox={"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0},
        normalized_aoi={"type": "Polygon", "coordinates": []},
        timing=recorder,
        stage_prefix="release_resolution",
        scene_role="single",
    )

    assert resolved.zoom == 18
    payload = recorder.to_dict()
    stage_names = [stage["name"] for stage in payload["stages"]]
    assert stage_names.count("release_resolution.tile_availability_preflight") == 2
    preflight_stages = [stage for stage in payload["stages"] if stage["name"] == "release_resolution.tile_availability_preflight"]
    assert preflight_stages[0]["metadata"]["cache_enabled"] is True
    assert preflight_stages[0]["metadata"]["cache_hit"] is False
    assert preflight_stages[0]["metadata"]["cache_key"].startswith("wayback-tile-preflight-")
    attempts = [stage for stage in payload["stages"] if stage["name"] == "release_resolution.zoom_attempt"]
    assert attempts[0]["metadata"]["preflight_status"] == "incomplete"
    assert attempts[1]["metadata"]["preflight_status"] == "complete"


def test_resolve_release_records_fallback_metadata_when_no_zoom_has_coverage(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, zoom=19, min_zoom=17, preflight=False)
    release = _release()
    recorder = StageTimingRecorder(run_id="run-3", pipeline_kind="detection")

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: _metadata_summary(usable=False),
    )

    resolved = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox={"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0},
        normalized_aoi={"type": "Polygon", "coordinates": []},
        timing=recorder,
        stage_prefix="release_resolution",
        scene_role="single",
    )

    assert resolved.zoom == 19
    payload = recorder.to_dict()
    decision = next(stage for stage in payload["stages"] if stage["name"] == "release_resolution.decision")
    assert decision["metadata"]["fallback_used"] is True
    assert decision["metadata"]["selected_zoom"] == 19
    total = next(stage for stage in payload["stages"] if stage["name"] == "release_resolution.total")
    assert total["metadata"]["attempt_count"] == 3
    assert total["metadata"]["attempted_zooms"] == [19, 18, 17]


def test_release_resolution_timing_failures_do_not_change_selection(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, zoom=19, min_zoom=18, preflight=False)
    release = _release()

    class FailingTiming:
        run_id = "failing-run"

        def add_stage(self, *args, **kwargs):
            raise RuntimeError("timing failed")

    monkeypatch.setattr("src.services.processing.build_session", lambda settings: _DummySession())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: _metadata_summary(usable=kwargs["zoom"] == 18),
    )

    resolved = _resolve_release_for_aoi(
        settings,
        release=release,
        aoi_bbox={"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0},
        normalized_aoi={"type": "Polygon", "coordinates": []},
        timing=FailingTiming(),
        stage_prefix="release_resolution",
        scene_role="single",
    )

    assert resolved.zoom == 18
