from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

from src.config import Settings
from src.domain.cache import save_cached_response
from src.domain.mapbox_current import MAPBOX_SOURCE_ID
from src.domain.mosaic import MosaicResult
from src.execution_profiles import PipelineExecutionConfig
from src.schemas import RunRequest, RunResponse, SummaryStats, TemporalProject
from src.services.temporal_projects import (
    _build_temporal_imagery_prefetch_plan,
    _plan_temporal_milestone_runs,
    _run_temporal_imagery_prefetch,
    run_temporal_project,
    save_temporal_project,
)
from src.services.validation import validate_request
from src.domain.wayback import WaybackRelease


def _sample_releases(settings: Settings) -> list[WaybackRelease]:
    return [
        WaybackRelease(
            identifier="WB_2024_R01",
            release_date=date(2024, 1, 1),
            label="2024-01-01 | WB_2024_R01",
            release_num=1,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2024",
        ),
        WaybackRelease(
            identifier="WB_2025_R01",
            release_date=date(2025, 1, 1),
            label="2025-01-01 | WB_2025_R01",
            release_num=2,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2025",
        ),
        WaybackRelease(
            identifier="WB_2026_R01",
            release_date=date(2026, 1, 1),
            label="2026-01-01 | WB_2026_R01",
            release_num=3,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2026",
        ),
    ]


def _sample_project(project_id: str = "temporal-prefetch") -> TemporalProject:
    return TemporalProject(
        project_id=project_id,
        name="Temporal Prefetch",
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[
                [-7.0, 33.0],
                [-6.998, 33.0],
                [-6.998, 33.002],
                [-7.0, 33.002],
                [-7.0, 33.0],
            ]],
        },
        milestones=[
            {"release_identifier": "WB_2024_R01"},
            {"release_identifier": "WB_2025_R01"},
            {"release_identifier": "WB_2026_R01"},
        ],
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )


def _mapbox_project(project_id: str = "temporal-prefetch-mapbox") -> TemporalProject:
    return TemporalProject(
        project_id=project_id,
        name="Temporal Prefetch Mapbox",
        aoi_geojson=_sample_project().aoi_geojson,
        milestones=[
            {"release_identifier": "WB_2025_R01"},
            {"release_identifier": "WB_2026_R01"},
        ],
        latest_source="mapbox_current",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )


def _bandon_config() -> PipelineExecutionConfig:
    return PipelineExecutionConfig(model_backend="bandon_mps")


def _bandon_pair_response(
    settings: Settings,
    request: RunRequest,
    *,
    releases: list[WaybackRelease],
) -> RunResponse:
    validation, prepared = validate_request(
        request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
        request_hash_context={
            "model_backend": "bandon_mps",
            "backend_mode": "bandon_mps",
            "bandon_processing_version": 2,
            "bandon_repo_dir": str(settings.bandon_repo_dir),
            "bandon_env_prefix": str(settings.bandon_env_prefix),
            "bandon_config_path": str(settings.bandon_config_path),
            "bandon_checkpoint_path": str(settings.bandon_checkpoint_path),
            "bandon_device": settings.bandon_device,
            "bandon_allow_mps_fallback": settings.bandon_allow_mps_fallback,
        },
    )
    assert prepared is not None
    assert not validation.blocking_errors
    return RunResponse(
        success=True,
        summary=SummaryStats(
            request_hash=prepared.request_hash,
            mode=request.mode,
            model_backend="bandon_mps",
            estimated_area_m2=1.0,
            tile_count_t1=1,
            tile_count_t2=1,
            total_new_buildings=1,
            total_building_blocks=1,
            total_new_building_area_m2=1.0,
            total_building_block_area_m2=1.0,
        ),
        new_buildings_geojson={
            "type": "FeatureCollection",
            "features": [],
        },
        building_blocks_geojson={
            "type": "FeatureCollection",
            "features": [],
        },
    )


def _fake_mosaic_result(cache_dir: Path, *, provider: str, identifier: str, source_type: str) -> MosaicResult:
    cache_dir.mkdir(parents=True, exist_ok=True)
    png_path = cache_dir / "mosaic.png"
    tif_path = cache_dir / "mosaic.tif"
    valid_mask_path = cache_dir / "valid_mask.tif"
    for path in (png_path, tif_path, valid_mask_path):
        path.write_bytes(b"test")
    return MosaicResult(
        identifier=identifier,
        release_date="2026-01-01",
        zoom=18,
        tile_count=1,
        available_tile_count=1,
        missing_tile_count=0,
        tile_range=(0, 0, 0, 0),
        bounds_3857=(0.0, 0.0, 1.0, 1.0),
        png_path=png_path,
        geotiff_path=tif_path,
        valid_mask_path=valid_mask_path,
        shared_cache_dir=cache_dir,
        cache_key=f"{provider}-{identifier}",
        provider=provider,
        source_type=source_type,
        source_id=identifier,
        effective_date="2026-01-01",
        capture_date_known=provider != "mapbox",
        metadata={"cache_hit": True},
    )


def test_temporal_imagery_prefetch_plan_builds_expected_pairs_for_mapbox_latest(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        temporal_imagery_prefetch_enabled=True,
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="test-token",
    )
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    project = save_temporal_project(_mapbox_project(), settings)

    plan = _plan_temporal_milestone_runs(
        project,
        settings=settings,
        remote_patch_budget_enabled=False,
        request_hash_context={"model_backend": "bandon_mps", "backend_mode": "bandon_mps"},
    )
    prefetch_plan = _build_temporal_imagery_prefetch_plan(project, plan, settings=settings)

    assert len(prefetch_plan) == 2
    assert [(item.t1_provider, item.t2_provider) for item in prefetch_plan] == [
        ("esri_wayback", "esri_wayback"),
        ("esri_wayback", "mapbox"),
    ]
    assert prefetch_plan[-1].t2_release_identifier == MAPBOX_SOURCE_ID
    assert prefetch_plan[-1].t2_effective_release_identifier == "WB_2026_R01"


def test_run_temporal_imagery_prefetch_uses_shared_cache_only_without_manifest_or_export(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        temporal_imagery_prefetch_enabled=True,
        temporal_imagery_prefetch_workers=1,
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="test-token",
    )
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    project = save_temporal_project(_mapbox_project("prefetch-no-manifest"), settings)
    plan = _plan_temporal_milestone_runs(
        project,
        settings=settings,
        remote_patch_budget_enabled=False,
        request_hash_context={"model_backend": "bandon_mps", "backend_mode": "bandon_mps"},
    )

    download_calls: list[tuple[str, str, bool, int]] = []

    def _fake_resolve(*args, **kwargs):
        return SimpleNamespace(zoom=18, tilemap=None)

    def _fake_wayback_download(self, release, bbox, *, settings, zoom, out_dir, label, available_tiles=None):
        download_calls.append(("esri_wayback", release.identifier, settings.materialize_source_imagery_in_requests, settings.download_workers))
        return _fake_mosaic_result(settings.wayback_mosaic_cache_dir / release.identifier, provider="esri_wayback", identifier=release.identifier, source_type="historical_release")

    def _fake_mapbox_download(self, bbox, *, settings, zoom=None):
        download_calls.append(("mapbox", MAPBOX_SOURCE_ID, settings.materialize_source_imagery_in_requests, settings.download_workers))
        return _fake_mosaic_result(settings.mapbox_current_imagery_cache_dir / "mapbox", provider="mapbox", identifier=MAPBOX_SOURCE_ID, source_type="current_basemap")

    monkeypatch.setattr("src.services.temporal_projects._resolve_release_for_aoi", _fake_resolve)
    monkeypatch.setattr("src.services.temporal_projects.EsriWaybackProvider.download", _fake_wayback_download)
    monkeypatch.setattr("src.services.temporal_projects.MapboxCurrentProvider.download", _fake_mapbox_download)

    results = _run_temporal_imagery_prefetch(project, settings=settings, pair_plan=plan)

    assert len(results) == 2
    assert all(item.status == "success" for item in results)
    assert all(materialized is False for _, _, materialized, _ in download_calls)
    assert all(worker_count == settings.download_workers for _, _, _, worker_count in download_calls)
    assert not any(settings.request_cache_dir.glob("*/manifest.json"))
    assert not any(settings.request_cache_dir.glob("*/export_bundle.zip"))
    assert list(settings.tmp_cache_dir.iterdir()) == []


def test_run_temporal_project_prefetches_before_serial_pair_runner_and_writes_timing(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        temporal_imagery_prefetch_enabled=True,
        temporal_imagery_prefetch_workers=1,
        model_backend_default="bandon_mps",
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="test-token",
    )
    releases = _sample_releases(settings)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)
    project = save_temporal_project(_mapbox_project("prefetch-run-order"), settings)

    events: list[str] = []

    def _fake_resolve(*args, **kwargs):
        return SimpleNamespace(zoom=18, tilemap=None)

    def _fake_wayback_download(self, release, bbox, *, settings, zoom, out_dir, label, available_tiles=None):
        events.append(f"prefetch:esri:{release.identifier}:{label}")
        return _fake_mosaic_result(settings.wayback_mosaic_cache_dir / f"{release.identifier}-{label}", provider="esri_wayback", identifier=release.identifier, source_type="historical_release")

    def _fake_mapbox_download(self, bbox, *, settings, zoom=None):
        events.append("prefetch:mapbox:mapbox.satellite")
        return _fake_mosaic_result(settings.mapbox_current_imagery_cache_dir / "mapbox-run", provider="mapbox", identifier=MAPBOX_SOURCE_ID, source_type="current_basemap")

    monkeypatch.setattr("src.services.temporal_projects._resolve_release_for_aoi", _fake_resolve)
    monkeypatch.setattr("src.services.temporal_projects.EsriWaybackProvider.download", _fake_wayback_download)
    monkeypatch.setattr("src.services.temporal_projects.MapboxCurrentProvider.download", _fake_mapbox_download)

    def _pair_runner(request: RunRequest) -> RunResponse:
        events.append(f"pair_runner:{request.t1_release}->{request.t2_release}:{request.latest_source}")
        response = _bandon_pair_response(settings, request, releases=releases)
        save_cached_response(settings, response.summary.request_hash, response)
        return response

    response = run_temporal_project(
        project.project_id,
        settings=settings,
        pair_runner=_pair_runner,
        execution_config=_bandon_config(),
    )

    assert response.success is True
    pair_events = [event for event in events if event.startswith("pair_runner:")]
    assert pair_events == [
        "pair_runner:WB_2025_R01->WB_2026_R01:esri_wayback",
        "pair_runner:WB_2026_R01->WB_2026_R01:mapbox_current",
    ]
    first_pair_event_index = events.index(pair_events[0])
    assert all(event.startswith("prefetch:") for event in events[:first_pair_event_index])

    timing_path = tmp_path / "temporal_projects" / project.project_id / "timing.json"
    assert timing_path.exists()
    timing_payload = json.loads(timing_path.read_text())
    stage_names = [stage["name"] for stage in timing_payload["stages"]]
    assert "temporal_imagery_prefetch_total" in stage_names
    assert "temporal_imagery_prefetch.pair_1" in stage_names
    assert "temporal_imagery_prefetch.pair_2" in stage_names
