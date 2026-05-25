from __future__ import annotations

from datetime import date
import json

import geopandas as gpd
from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.ops import unary_union

from src.config import Settings
from src.domain.cache import save_cached_response
from src.execution_profiles import PipelineExecutionConfig, resolve_backend
from src.schemas import PreviewImages, RunRequest, RunResponse, SummaryStats, TemporalMilestone, TemporalOverrideRequest, TemporalProject
from src.domain.vectorize import build_temporal_growth_blocks, build_temporal_growth_envelope
from src.services.releases import list_releases
from src.services.temporal_projects import (
    _reference_imagery_from_pair_response,
    audit_temporal_project_metadata_bloat,
    audit_temporal_project_metrics,
    get_temporal_project,
    list_temporal_projects,
    import_temporal_override,
    resolve_temporal_project_execution_config,
    publish_completed_tiled_request,
    run_temporal_project,
    save_temporal_project,
    validate_temporal_project,
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


def _sample_project(project_id: str = "temporal-demo") -> TemporalProject:
    return TemporalProject(
        project_id=project_id,
        name="Temporal Demo",
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
        created_at="2026-04-17T00:00:00Z",
        updated_at="2026-04-17T00:00:00Z",
    )


def _bandon_config() -> PipelineExecutionConfig:
    return PipelineExecutionConfig(inference_backend="bandon_mps")


def _bandon_pair_response(
    settings: Settings,
    request: RunRequest,
    *,
    releases: list[WaybackRelease],
    geojson: dict,
) -> RunResponse:
    backend = resolve_backend(PipelineExecutionConfig(inference_backend="bandon_mps"), settings=settings)
    configured_settings = backend.configure_settings(settings)
    validation, prepared = validate_request(
        request,
        releases=releases,
        settings=configured_settings,
        remote_patch_budget_enabled=False,
        request_hash_context=backend.request_hash_context(configured_settings),
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
        new_buildings_geojson=geojson,
        building_blocks_geojson=geojson,
    )


def _sample_releases_with_2027(settings: Settings) -> list[WaybackRelease]:
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
        WaybackRelease(
            identifier="WB_2027_R01",
            release_date=date(2027, 1, 1),
            label="2027-01-01 | WB_2027_R01",
            release_num=4,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2027",
        ),
    ]


def _feature_collection(coords: list[list[tuple[float, float]]]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [[*ring, ring[0]]]},
            }
            for ring in coords
        ],
    }


def _geometry(payload: dict) -> object:
    return unary_union([shape(feature["geometry"]) for feature in payload.get("features", []) if feature.get("geometry")]).buffer(0)


def _has_holes(geometry: object) -> bool:
    if isinstance(geometry, Polygon):
        return len(geometry.interiors) > 0
    if isinstance(geometry, MultiPolygon):
        return any(len(part.interiors) > 0 for part in geometry.geoms)
    return False


def test_validate_temporal_project_rejects_out_of_order_milestones(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    project = _sample_project("out-of-order")
    project.milestones = [
        project.milestones[1],
        project.milestones[0],
    ]

    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))

    response = validate_temporal_project(project, settings=settings)

    assert response.valid is False
    assert any("chronological order" in message for message in response.blocking_errors)


def test_run_temporal_project_builds_monotonic_cumulative_union(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    project = save_temporal_project(_sample_project("monotonic-growth"), settings)

    automated_layers = {
        "WB_2025_R01": _feature_collection(
            [[(-6.9998, 33.0002), (-6.9992, 33.0002), (-6.9992, 33.0008), (-6.9998, 33.0008)]]
        ),
        "WB_2026_R01": _feature_collection(
            [[(-6.9989, 33.0011), (-6.9984, 33.0011), (-6.9984, 33.0016), (-6.9989, 33.0016)]]
        ),
    }

    def _pair_runner(request):
        return RunResponse(
            success=True,
            summary=SummaryStats(
                request_hash=f"{request.t1_release}-{request.t2_release}",
                mode=request.mode,
                estimated_area_m2=1.0,
                tile_count_t1=1,
                tile_count_t2=1,
                total_new_buildings=1,
                total_building_blocks=1,
                total_new_building_area_m2=1.0,
                total_building_block_area_m2=1.0,
            ),
            new_buildings_geojson=automated_layers[request.t2_release],
            building_blocks_geojson=automated_layers[request.t2_release],
        )

    response = run_temporal_project(project.project_id, settings=settings, pair_runner=_pair_runner)

    assert response.success is True
    assert response.project.milestones[0].metrics is not None
    assert response.project.milestones[1].metrics is not None
    assert response.project.milestones[2].metrics is not None
    assert response.project.milestones[0].metrics.total_area_m2 == 0.0
    assert response.project.milestones[1].metrics.total_area_m2 > response.project.milestones[0].metrics.total_area_m2
    assert response.project.milestones[2].metrics.total_area_m2 > response.project.milestones[1].metrics.total_area_m2
    assert response.project.milestones[1].cumulative_union_geojson is not None
    assert response.project.milestones[2].cumulative_union_geojson is not None
    assert _geometry(response.project.milestones[1].cumulative_union_geojson).within(_geometry(response.project.milestones[2].cumulative_union_geojson))
    final_cumulative = _geometry(response.project.milestones[2].cumulative_union_geojson)
    final_envelope_geojson = response.project.milestones[2].cumulative_growth_envelope_geojson
    assert final_envelope_geojson is not None
    assert len(final_envelope_geojson["features"]) == 1
    final_envelope = _geometry(final_envelope_geojson)
    assert not _has_holes(final_envelope)
    assert final_cumulative.difference(final_envelope).area <= 1e-14


def test_publish_completed_tiled_request_uses_temporal_project_artifact_schema(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    project = _sample_project("publish-tiled")
    project.milestones = [
        TemporalMilestone(release_identifier="WB_2024_R01"),
        TemporalMilestone(release_identifier="WB_2025_R01"),
    ]
    saved = save_temporal_project(project, settings)

    request_id = "completed-tiled-request"
    request_dir = settings.request_cache_dir / request_id
    request_dir.mkdir(parents=True)
    (request_dir / "prediction_change_mask.tif").write_bytes(b"mask")
    (request_dir / "prediction_change_probability.tif").write_bytes(b"probability")
    (request_dir / "export_bundle.zip").write_bytes(b"zip")
    change_geojson = _feature_collection(
        [[(-6.9998, 33.0002), (-6.9992, 33.0002), (-6.9992, 33.0008), (-6.9998, 33.0008)]]
    )
    (request_dir / "building_change_polygons.geojson").write_text(json.dumps(change_geojson), encoding="utf-8")
    save_cached_response(
        settings,
        request_id,
        RunResponse(
            success=True,
            summary=SummaryStats(
                request_hash=request_id,
                mode="full_run",
                model_backend="bandon_mps",
                estimated_area_m2=1.0,
                tile_count_t1=1,
                tile_count_t2=1,
                total_new_buildings=0,
                total_building_blocks=0,
                total_new_building_area_m2=0.0,
                total_building_block_area_m2=0.0,
                total_change_polygons=1,
                total_change_area_m2=1.0,
            ),
            change_polygons_geojson=change_geojson,
            downloadable_zip_path=str(request_dir / "export_bundle.zip"),
        ),
    )

    result = publish_completed_tiled_request(
        request_id=request_id,
        project_id=saved.project_id,
        target_release="WB_2025_R01",
        baseline_release="WB_2024_R01",
        settings=settings,
    )
    second_result = publish_completed_tiled_request(
        request_id=request_id,
        project_id=saved.project_id,
        target_release="WB_2025_R01",
        baseline_release="WB_2024_R01",
        settings=settings,
    )

    milestone_dir = settings.temporal_projects_dir / saved.project_id / "milestones" / "WB_2025_R01"
    assert (milestone_dir / "additions.geojson").is_file()
    assert (milestone_dir / "building_change_buffer_10m.geojson").is_file()
    assert (milestone_dir / "building_change_buffer_15m.geojson").is_file()
    assert (milestone_dir / "building_change_buffer_20m.geojson").is_file()
    assert (settings.temporal_projects_dir / saved.project_id / "project_manifest.json").is_file()
    assert (settings.temporal_projects_dir / saved.project_id / "project_summary.json").is_file()
    assert result["artifact_counts"]["additions.geojson"] == 1
    assert second_result["artifact_counts"]["additions.geojson"] == 1
    reloaded = get_temporal_project(saved.project_id, settings)
    target = next(item for item in reloaded.milestones if item.release_identifier == "WB_2025_R01")
    assert target.pair_request_hash == request_id
    artifact_names = {artifact.name for artifact in target.artifacts}
    assert "WB_2025_R01_export_bundle" in artifact_names


def test_temporal_project_metric_audit_reads_published_artifacts(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    project = _sample_project("metric-audit")
    project.milestones = [
        TemporalMilestone(release_identifier="WB_2024_R01"),
        TemporalMilestone(release_identifier="WB_2025_R01"),
    ]
    additions_geojson = _feature_collection(
        [[(-6.9998, 33.0002), (-6.9992, 33.0002), (-6.9992, 33.0008), (-6.9998, 33.0008)]]
    )
    project.milestones[1].additions_geojson = additions_geojson
    saved = save_temporal_project(project, settings)
    milestone_dir = settings.temporal_projects_dir / saved.project_id / "milestones" / "WB_2025_R01"
    milestone_dir.mkdir(parents=True, exist_ok=True)
    (milestone_dir / "additions.geojson").write_text(json.dumps(additions_geojson), encoding="utf-8")

    result = audit_temporal_project_metrics(
        project_id=saved.project_id,
        target_release="WB_2025_R01",
        settings=settings,
    )

    assert result["layers"]["additions"]["feature_count"] == 1
    assert result["layers"]["additions"]["geometry_area_m2"] > 0
    assert result["ui_added_area_m2"] >= 0


def test_temporal_project_metadata_bloat_audit_externalizes_feature_collections(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    saved = save_temporal_project(_sample_project("metadata-audit"), settings)
    project_json = settings.temporal_projects_dir / saved.project_id / "project.json"
    result = audit_temporal_project_metadata_bloat(
        project_id=saved.project_id,
        settings=settings,
        repair_metadata=True,
        threshold_bytes=1,
    )

    assert result["bloated"] is True
    assert result["repair_metadata_requested"] is True
    assert result["repair_metadata_applied"] is True
    assert result["project_json_after_bytes"] <= result["project_json_before_bytes"]


def test_run_temporal_project_infers_legacy_bandon_execution_config_and_skips_reruns(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    releases = _sample_releases(settings)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)
    project = save_temporal_project(_sample_project("legacy-bandon"), settings)

    automated_layers = {
        "WB_2025_R01": _feature_collection(
            [[(-6.9998, 33.0002), (-6.9994, 33.0002), (-6.9994, 33.0006), (-6.9998, 33.0006)]]
        ),
        "WB_2026_R01": _feature_collection(
            [[(-6.9992, 33.0008), (-6.9988, 33.0008), (-6.9988, 33.0012), (-6.9992, 33.0012)]]
        ),
    }
    executed_pairs: list[tuple[str, str]] = []

    def _pair_runner(request):
        executed_pairs.append((request.t1_release, request.t2_release))
        response = _bandon_pair_response(
            settings,
            request,
            releases=releases,
            geojson=automated_layers[request.t2_release],
        )
        save_cached_response(settings, response.summary.request_hash, response)
        return response

    initial = run_temporal_project(
        project.project_id,
        settings=settings,
        pair_runner=_pair_runner,
        execution_config=_bandon_config(),
    )
    assert initial.success is True
    assert executed_pairs == [("WB_2024_R01", "WB_2025_R01"), ("WB_2025_R01", "WB_2026_R01")]

    project_json_path = settings.temporal_projects_dir / project.project_id / "project.json"
    legacy_payload = json.loads(project_json_path.read_text())
    legacy_payload.pop("execution_config", None)
    project_json_path.write_text(json.dumps(legacy_payload, indent=2))

    inferred = resolve_temporal_project_execution_config(get_temporal_project(project.project_id, settings), settings)
    assert inferred.inference_backend == "bandon_mps"

    executed_pairs.clear()
    rerun = run_temporal_project(project.project_id, settings=settings, pair_runner=_pair_runner)
    assert rerun.success is True
    assert rerun.project.execution_config is not None
    assert rerun.project.execution_config.inference_backend == "bandon_mps"
    assert executed_pairs == []


def test_run_temporal_project_only_executes_appended_milestone(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    releases = _sample_releases(settings)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)
    project = save_temporal_project(
        TemporalProject(
            project_id="append-only",
            name="Append Only",
            aoi_geojson=_sample_project().aoi_geojson,
            milestones=[
                {"release_identifier": "WB_2024_R01"},
                {"release_identifier": "WB_2025_R01"},
            ],
            created_at="2026-04-20T00:00:00Z",
            updated_at="2026-04-20T00:00:00Z",
        ),
        settings,
    )

    automated_layers = {
        "WB_2025_R01": _feature_collection(
            [[(-6.9998, 33.0002), (-6.9994, 33.0002), (-6.9994, 33.0006), (-6.9998, 33.0006)]]
        ),
        "WB_2026_R01": _feature_collection(
            [[(-6.9992, 33.0008), (-6.9988, 33.0008), (-6.9988, 33.0012), (-6.9992, 33.0012)]]
        ),
    }
    executed_pairs: list[tuple[str, str]] = []

    def _pair_runner(request):
        executed_pairs.append((request.t1_release, request.t2_release))
        response = _bandon_pair_response(
            settings,
            request,
            releases=releases,
            geojson=automated_layers[request.t2_release],
        )
        save_cached_response(settings, response.summary.request_hash, response)
        return response

    first_run = run_temporal_project(
        project.project_id,
        settings=settings,
        pair_runner=_pair_runner,
        execution_config=_bandon_config(),
    )
    assert first_run.success is True
    assert executed_pairs == [("WB_2024_R01", "WB_2025_R01")]

    saved_project = get_temporal_project(project.project_id, settings)
    saved_project.milestones.append(TemporalMilestone(release_identifier="WB_2026_R01"))
    save_temporal_project(saved_project, settings)

    executed_pairs.clear()
    second_run = run_temporal_project(project.project_id, settings=settings, pair_runner=_pair_runner)
    assert second_run.success is True
    assert executed_pairs == [("WB_2025_R01", "WB_2026_R01")]
    assert second_run.project.milestones[1].pair_request_hash == first_run.project.milestones[1].pair_request_hash
    assert second_run.project.milestones[2].pair_request_hash is not None


def test_mapbox_latest_source_adds_synthetic_latest_milestone(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="test-token",
    )
    releases = _sample_releases(settings)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)
    project = save_temporal_project(
        TemporalProject(
            project_id="mapbox-latest",
            name="Mapbox Latest",
            aoi_geojson=_sample_project().aoi_geojson,
            milestones=[
                {"release_identifier": "WB_2025_R01"},
                {"release_identifier": "WB_2026_R01"},
            ],
            latest_source="mapbox_current",
            created_at="2026-04-20T00:00:00Z",
            updated_at="2026-04-20T00:00:00Z",
        ),
        settings,
    )

    assert [milestone.release_identifier for milestone in project.milestones] == [
        "WB_2025_R01",
        "WB_2026_R01",
        "mapbox.satellite",
    ]
    assert project.milestones[-1].release_date == "current_basemap"

    validation = validate_temporal_project(
        project,
        settings=settings,
        remote_patch_budget_enabled=False,
        request_hash_context={"model_backend": "bandon_mps", "inference_backend": "bandon_mps"},
        execution_config=_bandon_config(),
    )

    assert validation.valid is True
    assert [estimate.to_release_identifier for estimate in validation.pair_estimates] == [
        "WB_2026_R01",
        "mapbox.satellite",
    ]


def test_mapbox_latest_source_runs_after_latest_wayback_milestone(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="test-token",
    )
    releases = _sample_releases(settings)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)
    project = save_temporal_project(
        TemporalProject(
            project_id="mapbox-run",
            name="Mapbox Run",
            aoi_geojson=_sample_project().aoi_geojson,
            milestones=[
                {"release_identifier": "WB_2025_R01"},
                {"release_identifier": "WB_2026_R01"},
            ],
            latest_source="mapbox_current",
            created_at="2026-04-20T00:00:00Z",
            updated_at="2026-04-20T00:00:00Z",
        ),
        settings,
    )
    executed_pairs: list[tuple[str, str, str]] = []

    def _pair_runner(request):
        executed_pairs.append((request.t1_release, request.t2_release, request.latest_source))
        response = _bandon_pair_response(
            settings,
            request,
            releases=releases,
            geojson=_feature_collection(
                [[(-6.9998, 33.0002), (-6.9994, 33.0002), (-6.9994, 33.0006), (-6.9998, 33.0006)]]
            ),
        )
        save_cached_response(settings, response.summary.request_hash, response)
        return response

    response = run_temporal_project(
        project.project_id,
        settings=settings,
        pair_runner=_pair_runner,
        execution_config=_bandon_config(),
    )

    assert response.success is True
    assert executed_pairs == [
        ("WB_2025_R01", "WB_2026_R01", "esri_wayback"),
        ("WB_2026_R01", "WB_2026_R01", "mapbox_current"),
    ]
    assert response.project.milestones[-1].release_identifier == "mapbox.satellite"
    assert response.project.milestones[-1].status == "complete"
    assert response.project.milestones[-1].pair_request_hash is not None


def test_run_temporal_project_reruns_only_dirty_prefix_boundary(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    releases = _sample_releases_with_2027(settings)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)
    project = save_temporal_project(
        TemporalProject(
            project_id="mid-sequence",
            name="Mid Sequence",
            aoi_geojson=_sample_project().aoi_geojson,
            milestones=[
                {"release_identifier": "WB_2024_R01"},
                {"release_identifier": "WB_2026_R01"},
                {"release_identifier": "WB_2027_R01"},
            ],
            created_at="2026-04-20T00:00:00Z",
            updated_at="2026-04-20T00:00:00Z",
        ),
        settings,
    )

    automated_layers = {
        "WB_2025_R01": _feature_collection(
            [[(-6.9998, 33.0002), (-6.9994, 33.0002), (-6.9994, 33.0006), (-6.9998, 33.0006)]]
        ),
        "WB_2026_R01": _feature_collection(
            [[(-6.9992, 33.0008), (-6.9988, 33.0008), (-6.9988, 33.0012), (-6.9992, 33.0012)]]
        ),
        "WB_2027_R01": _feature_collection(
            [[(-6.9986, 33.0013), (-6.9982, 33.0013), (-6.9982, 33.0017), (-6.9986, 33.0017)]]
        ),
    }
    executed_pairs: list[tuple[str, str]] = []

    def _pair_runner(request):
        executed_pairs.append((request.t1_release, request.t2_release))
        response = _bandon_pair_response(
            settings,
            request,
            releases=releases,
            geojson=automated_layers[request.t2_release],
        )
        save_cached_response(settings, response.summary.request_hash, response)
        return response

    initial = run_temporal_project(
        project.project_id,
        settings=settings,
        pair_runner=_pair_runner,
        execution_config=_bandon_config(),
    )
    assert initial.success is True
    assert executed_pairs == [("WB_2024_R01", "WB_2026_R01"), ("WB_2026_R01", "WB_2027_R01")]

    saved_project = get_temporal_project(project.project_id, settings)
    saved_project.milestones.insert(1, TemporalMilestone(release_identifier="WB_2025_R01"))
    save_temporal_project(saved_project, settings)

    executed_pairs.clear()
    rerun = run_temporal_project(project.project_id, settings=settings, pair_runner=_pair_runner)
    assert rerun.success is True
    assert executed_pairs == [("WB_2024_R01", "WB_2025_R01"), ("WB_2025_R01", "WB_2026_R01")]
    assert rerun.project.milestones[3].pair_request_hash == initial.project.milestones[2].pair_request_hash


def test_validate_temporal_project_clears_stale_baseline_pair_hash(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    project = _sample_project("baseline-normalized")
    project.milestones[0].pair_request_hash = "stale-hash"
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))

    response = validate_temporal_project(project, settings=settings)

    assert response.project.milestones[0].pair_request_hash is None


def test_temporal_growth_blocks_cluster_nearby_polygons_into_one_block() -> None:
    aoi_geojson = _sample_project().aoi_geojson
    assert aoi_geojson is not None

    source_geojson = _feature_collection(
        [
            [(-7.00000, 33.00000), (-6.99996, 33.00000), (-6.99996, 33.00004), (-7.00000, 33.00004)],
            [(-6.99990, 33.00000), (-6.99986, 33.00000), (-6.99986, 33.00004), (-6.99990, 33.00004)],
        ]
    )

    blocks_df, blocks_geojson = build_temporal_growth_blocks(
        source_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier="WB_2025_R01",
        release_date="2025-01-01",
        kind="effective_building_block",
    )

    assert not blocks_df.empty
    assert len(blocks_df) == 1
    assert len(blocks_geojson["features"]) == 1
    properties = blocks_geojson["features"][0]["properties"]
    assert properties["block_id"] == 1
    assert properties["source_building_count"] == 2
    assert properties["cluster_gap_m"] == 20.0
    assert properties["kind"] == "effective_building_block"
    assert blocks_geojson["features"][0]["geometry"]["type"] == "Polygon"
    assert shape(blocks_geojson["features"][0]["geometry"]).area > _geometry(source_geojson).area


def test_temporal_growth_blocks_leave_distant_polygons_separate() -> None:
    aoi_geojson = _sample_project().aoi_geojson
    assert aoi_geojson is not None

    source_geojson = _feature_collection(
        [
            [(-7.00000, 33.00000), (-6.99996, 33.00000), (-6.99996, 33.00004), (-7.00000, 33.00004)],
            [(-6.99960, 33.00000), (-6.99956, 33.00000), (-6.99956, 33.00004), (-6.99960, 33.00004)],
        ]
    )

    blocks_df, blocks_geojson = build_temporal_growth_blocks(
        source_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier="WB_2025_R01",
        release_date="2025-01-01",
        kind="effective_building_block",
    )

    assert len(blocks_df) == 2
    assert len(blocks_geojson["features"]) == 2
    assert set(blocks_df["source_building_count"]) == {1}


def test_temporal_growth_blocks_respect_road_barriers(tmp_path) -> None:
    aoi_geojson = _sample_project().aoi_geojson
    assert aoi_geojson is not None

    source_geojson = _feature_collection(
        [
            [(-7.00000, 33.00000), (-6.99996, 33.00000), (-6.99996, 33.00004), (-7.00000, 33.00004)],
            [(-6.99990, 33.00000), (-6.99986, 33.00000), (-6.99986, 33.00004), (-6.99990, 33.00004)],
        ]
    )
    roads_path = tmp_path / "roads.geojson"
    roads_gdf = gpd.GeoDataFrame(
        geometry=[LineString([(-6.99993, 32.99990), (-6.99993, 33.00015)])],
        crs="EPSG:4326",
    )
    roads_gdf.to_file(roads_path, driver="GeoJSON")

    blocks_df, blocks_geojson = build_temporal_growth_blocks(
        source_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier="WB_2025_R01",
        release_date="2025-01-01",
        kind="effective_building_block",
        road_constraint_layer_path=str(roads_path),
    )

    assert len(blocks_df) == 2
    assert len(blocks_geojson["features"]) == 2


def test_temporal_growth_envelope_is_clipped_to_aoi_and_not_smaller_than_blocks() -> None:
    aoi_geojson = _sample_project().aoi_geojson
    assert aoi_geojson is not None

    source_geojson = _feature_collection(
        [
            [(-6.99816, 33.00180), (-6.99810, 33.00180), (-6.99810, 33.00186), (-6.99816, 33.00186)],
            [(-6.99807, 33.00180), (-6.99801, 33.00180), (-6.99801, 33.00186), (-6.99807, 33.00186)],
        ]
    )

    blocks_df, blocks_geojson = build_temporal_growth_blocks(
        source_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier="WB_2025_R01",
        release_date="2025-01-01",
        kind="cumulative_growth_block",
    )
    envelope_df, envelope_geojson = build_temporal_growth_envelope(
        blocks_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier="WB_2025_R01",
        release_date="2025-01-01",
    )

    assert not blocks_df.empty
    assert not envelope_df.empty
    aoi_geometry = shape(aoi_geojson)
    block_geometry = _geometry(blocks_geojson)
    envelope_geometry = _geometry(envelope_geojson)
    assert envelope_geometry.difference(aoi_geometry).is_empty
    assert block_geometry.difference(envelope_geometry).area <= 1e-14


def test_temporal_growth_envelope_builds_one_no_hole_concave_polygon_covering_cumulative_union() -> None:
    aoi_geojson = _sample_project().aoi_geojson
    assert aoi_geojson is not None

    cumulative_geojson = _feature_collection(
        [
            [(-6.99982, 33.00018), (-6.99972, 33.00018), (-6.99972, 33.00028), (-6.99982, 33.00028)],
            [(-6.99922, 33.00018), (-6.99912, 33.00018), (-6.99912, 33.00028), (-6.99922, 33.00028)],
            [(-6.99952, 33.00078), (-6.99942, 33.00078), (-6.99942, 33.00088), (-6.99952, 33.00088)],
        ]
    )

    envelope_df, envelope_geojson = build_temporal_growth_envelope(
        cumulative_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier="WB_2026_R01",
        release_date="2026-01-01",
        envelope_hull_ratio=0.12,
    )

    cumulative_geometry = _geometry(cumulative_geojson)
    envelope_geometry = _geometry(envelope_geojson)

    assert not envelope_df.empty
    assert len(envelope_geojson["features"]) == 1
    assert envelope_geometry.geom_type == "Polygon"
    assert not _has_holes(envelope_geometry)
    assert cumulative_geometry.difference(envelope_geometry).area <= 1e-14
    assert envelope_geometry.area > cumulative_geometry.area
    assert envelope_geojson["features"][0]["properties"]["kind"] == "cumulative_growth_envelope"
    assert envelope_geojson["features"][0]["properties"]["envelope_method"] in {"concave_hull", "convex_hull_fallback"}


def test_reference_imagery_hydration_falls_back_to_png_data_url_from_image_path(tmp_path) -> None:
    png_path = tmp_path / "reference.png"
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00")

    response = RunResponse(
        success=True,
        summary=SummaryStats(
            request_hash="demo",
            mode="fast_preview",
            estimated_area_m2=1.0,
            tile_count_t1=1,
            tile_count_t2=1,
            total_new_buildings=1,
            total_building_blocks=1,
            total_new_building_area_m2=1.0,
            total_building_block_area_m2=1.0,
        ),
        preview_images=PreviewImages(
            t1_preview_path=str(png_path),
            raster_bounds_wgs84=[-7.0, 33.0, -6.99, 33.01],
        ),
    )

    reference_imagery = _reference_imagery_from_pair_response(response, use_t1_preview=True)

    assert reference_imagery is not None
    assert reference_imagery.image_path == str(png_path)
    assert reference_imagery.image_png_data_url is not None
    assert reference_imagery.image_png_data_url.startswith("data:image/png;base64,")


def test_list_temporal_projects_deduplicates_saved_projects_and_hides_cached_pairwise_by_default(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    saved_project = save_temporal_project(_sample_project("temporal-listing"), settings)

    cached_response = RunResponse(
        success=True,
        summary=SummaryStats(
            request_hash="pairwise-cache",
            mode="full_run",
            estimated_area_m2=1.0,
            tile_count_t1=1,
            tile_count_t2=1,
            total_new_buildings=1,
            total_building_blocks=1,
            total_new_building_area_m2=1.0,
            total_building_block_area_m2=1.0,
            release_date_t1="2024-01-01",
            release_date_t2="2025-01-01",
        ),
        preview_images=PreviewImages(
            raster_bounds_wgs84=[-7.0, 33.0, -6.99, 33.01],
        ),
    )
    save_cached_response(settings, "pairwise-cache", cached_response)

    default_summaries = list_temporal_projects(settings)
    assert [summary.project_id for summary in default_summaries] == [saved_project.project_id]

    cached_summaries = list_temporal_projects(settings, include_cached_runs=True)
    assert [summary.project_id for summary in cached_summaries] == [
        saved_project.project_id,
        "run-pairwise-cache",
    ]


def test_save_temporal_project_avoids_overwriting_another_project_in_the_same_directory(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    shared_directory = str(tmp_path / "shared-project-dir")

    first_project = save_temporal_project(_sample_project("first-project"), settings)
    first_project.project_dir = shared_directory
    saved_first = save_temporal_project(first_project, settings)

    second_project = _sample_project("second-project")
    second_project.project_dir = shared_directory
    saved_second = save_temporal_project(second_project, settings)

    assert saved_first.project_dir == shared_directory
    assert saved_second.project_dir == str(tmp_path / "shared-project-dir" / "second-project")
    assert get_temporal_project(saved_first.project_id, settings).project_id == "first-project"
    assert get_temporal_project(saved_second.project_id, settings).project_id == "second-project"


def test_four_milestone_run_keeps_a_single_saved_temporal_project_entry(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    releases = [
        WaybackRelease(
            identifier="WB_2020_R01",
            release_date=date(2020, 1, 1),
            label="2020-01-01 | WB_2020_R01",
            release_num=1,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2020",
        ),
        WaybackRelease(
            identifier="WB_2022_R01",
            release_date=date(2022, 1, 1),
            label="2022-01-01 | WB_2022_R01",
            release_num=2,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2022",
        ),
        WaybackRelease(
            identifier="WB_2024_R01",
            release_date=date(2024, 1, 1),
            label="2024-01-01 | WB_2024_R01",
            release_num=3,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2024",
        ),
        WaybackRelease(
            identifier="WB_2026_R01",
            release_date=date(2026, 1, 1),
            label="2026-01-01 | WB_2026_R01",
            release_num=4,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/2026",
        ),
    ]
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: releases)

    project = save_temporal_project(
        TemporalProject(
            project_id="temporal-hay-hassani",
            name="Hay Hassani",
            aoi_geojson=_sample_project().aoi_geojson,
            milestones=[
                {"release_identifier": "WB_2020_R01"},
                {"release_identifier": "WB_2022_R01"},
                {"release_identifier": "WB_2024_R01"},
                {"release_identifier": "WB_2026_R01"},
            ],
            created_at="2026-04-20T00:00:00Z",
            updated_at="2026-04-20T00:00:00Z",
        ),
        settings,
    )

    automated_layers = {
        "WB_2022_R01": _feature_collection(
            [[(-6.9998, 33.0002), (-6.9994, 33.0002), (-6.9994, 33.0006), (-6.9998, 33.0006)]]
        ),
        "WB_2024_R01": _feature_collection(
            [[(-6.9993, 33.0007), (-6.9989, 33.0007), (-6.9989, 33.0011), (-6.9993, 33.0011)]]
        ),
        "WB_2026_R01": _feature_collection(
            [[(-6.9988, 33.0012), (-6.9984, 33.0012), (-6.9984, 33.0016), (-6.9988, 33.0016)]]
        ),
    }

    def _pair_runner(request):
        return RunResponse(
            success=True,
            summary=SummaryStats(
                request_hash=f"{request.t1_release}-{request.t2_release}",
                mode=request.mode,
                estimated_area_m2=1.0,
                tile_count_t1=1,
                tile_count_t2=1,
                total_new_buildings=1,
                total_building_blocks=1,
                total_new_building_area_m2=1.0,
                total_building_block_area_m2=1.0,
            ),
            new_buildings_geojson=automated_layers[request.t2_release],
            building_blocks_geojson=automated_layers[request.t2_release],
        )

    response = run_temporal_project(project.project_id, settings=settings, pair_runner=_pair_runner)

    assert response.success is True
    final_cumulative_geojson = response.project.milestones[-1].cumulative_union_geojson
    final_convex_hull_geojson = response.project.milestones[-1].cumulative_convex_hull_geojson
    assert final_cumulative_geojson is not None
    assert final_convex_hull_geojson is not None
    final_cumulative = _geometry(final_cumulative_geojson)
    final_convex_hull = _geometry(final_convex_hull_geojson)
    assert final_convex_hull.geom_type == "Polygon"
    assert not _has_holes(final_convex_hull)
    assert final_cumulative.difference(final_convex_hull).area <= 1e-14
    summaries = list_temporal_projects(settings)
    assert [summary.project_id for summary in summaries] == ["temporal-hay-hassani"]
    assert summaries[0].display_name == "Temporal mosaic · Hay Hassani"
    assert summaries[0].milestone_count == 4


def test_import_temporal_override_recomputes_downstream_milestones(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    monkeypatch.setattr("src.services.temporal_projects.list_releases", lambda _: _sample_releases(settings))
    project = save_temporal_project(_sample_project("override-demo"), settings)

    automated_layers = {
        "WB_2025_R01": _feature_collection(
            [
                [(-6.99990, 33.00020), (-6.99986, 33.00020), (-6.99986, 33.00024), (-6.99990, 33.00024)],
                [(-6.99980, 33.00020), (-6.99976, 33.00020), (-6.99976, 33.00024), (-6.99980, 33.00024)],
            ]
        ),
        "WB_2026_R01": _feature_collection(
            [[(-6.99948, 33.00020), (-6.99942, 33.00020), (-6.99942, 33.00026), (-6.99948, 33.00026)]]
        ),
    }

    def _pair_runner(request):
        return RunResponse(
            success=True,
            summary=SummaryStats(
                request_hash=f"{request.t1_release}-{request.t2_release}",
                mode=request.mode,
                estimated_area_m2=1.0,
                tile_count_t1=1,
                tile_count_t2=1,
                total_new_buildings=1,
                total_building_blocks=1,
                total_new_building_area_m2=1.0,
                total_building_block_area_m2=1.0,
            ),
            new_buildings_geojson=automated_layers[request.t2_release],
            building_blocks_geojson=automated_layers[request.t2_release],
        )

    first_run = run_temporal_project(project.project_id, settings=settings, pair_runner=_pair_runner)
    assert first_run.success is True
    before_override_total = first_run.project.milestones[2].metrics.total_area_m2
    before_override_block_count = first_run.project.milestones[2].metrics.cumulative_block_count
    before_override_blocks = first_run.project.milestones[2].cumulative_growth_blocks_geojson

    override_response = import_temporal_override(
        TemporalOverrideRequest(
            project_id=project.project_id,
            release_identifier="WB_2025_R01",
            override_geojson=_feature_collection(
                [[(-6.99998, 33.00018), (-6.99938, 33.00018), (-6.99938, 33.00028), (-6.99998, 33.00028)]]
            ),
        ),
        settings=settings,
    )

    assert override_response.success is True
    assert override_response.project.milestones[1].source_mode == "hybrid_reviewed"
    assert override_response.project.milestones[2].metrics is not None
    assert override_response.project.milestones[2].metrics.total_area_m2 > before_override_total
    assert override_response.project.milestones[2].metrics.cumulative_block_count < before_override_block_count
    assert override_response.project.milestones[2].cumulative_growth_blocks_geojson != before_override_blocks

    reloaded = get_temporal_project(project.project_id, settings)
    assert reloaded.milestones[1].manual_override_geojson is not None
