from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings
from src.services import temporal_projects as service


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_geojson(path: Path, feature_count: int) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"index": index},
                "geometry": {
                    "type": "Point",
                    "coordinates": [-5.8 + index * 0.001, 35.75],
                },
            }
            for index in range(feature_count)
        ],
    }
    _write_json(path, payload)


def _write_project_summary(project_dir: Path, project_id: str) -> None:
    _write_json(
        project_dir / "project_summary.json",
        {
            "project_id": project_id,
            "name": "Tanger city",
            "project_dir": str(project_dir),
            "project_kind": "temporal",
            "display_name": "Temporal mosaic · Tanger city",
            "semantics": "expansion_only",
            "milestone_count": 2,
            "complete_milestone_count": 2,
            "created_at": "2026-06-21T13:49:09Z",
            "updated_at": "2026-06-21T23:44:07Z",
            "download_bundle_path": None,
        },
    )


def test_compact_project_detail_returns_tile_metadata_without_legacy_project_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    project_id = "temporal-tanger-city-mqnueqrr-llwf6o"
    project_dir = settings.temporal_projects_dir / project_id
    milestone_dir = project_dir / "milestones" / "WB_2026_R05"
    milestone_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text("{not valid full project json", encoding="utf-8")
    _write_project_summary(project_dir, project_id)
    (milestone_dir / "reference_imagery_cog.tif").write_bytes(b"fake-cog")
    _write_geojson(milestone_dir / "additions.geojson", 1)
    monkeypatch.setattr(service, "TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES", 10)
    monkeypatch.setattr(
        service,
        "_compact_reference_cog_bounds_wgs84",
        lambda path: [-5.91, 35.70, -5.74, 35.80],
    )
    monkeypatch.setattr(
        service,
        "_load_project",
        lambda *args, **kwargs: pytest.fail("compact loading must not call the legacy project loader"),
    )

    payload = service.load_temporal_project_compact_payload(project_id, settings)

    assert payload["id"] == project_id
    assert payload["loading_mode"] == "compact"
    assert payload["bounds"] == [-5.91, 35.70, -5.74, 35.80]
    assert payload["center"] == pytest.approx([-5.825, 35.75])
    assert payload["aoi_geojson"]["type"] == "Polygon"
    assert payload["milestones"][0]["bounds"] == [-5.91, 35.70, -5.74, 35.80]
    assert payload["milestones"][0]["reference_imagery"]["tilejson_url"].endswith("/reference/tilejson.json")
    assert payload["milestones"][0]["reference_imagery"]["raster_bounds_wgs84"] == [-5.91, 35.70, -5.74, 35.80]
    additions = payload["milestones"][0]["artifacts"]["additions"]
    assert additions["exists"] is True
    assert additions["tilejson_url"].endswith("/artifacts/additions/tilejson.json")
    assert additions["tiles_url_template"].endswith("/artifacts/additions/tiles/{z}/{x}/{y}.mvt")
    assert additions["source_layer"] == "results"


def test_compact_project_detail_marks_empty_artifacts_without_tilejson(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    project_id = "temporal-empty-artifact"
    project_dir = settings.temporal_projects_dir / project_id
    milestone_dir = project_dir / "milestones" / "WB_2026_R01"
    milestone_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text("{not parsed", encoding="utf-8")
    _write_project_summary(project_dir, project_id)
    _write_geojson(milestone_dir / "automated_building_blocks.geojson", 0)

    payload = service.load_temporal_project_compact_payload(project_id, settings)

    artifact = payload["milestones"][0]["artifacts"]["automated_building_blocks"]
    assert artifact["exists"] is False
    assert artifact["empty"] is True
    assert artifact["tilejson_url"] is None
    assert artifact["geojson_fallback_url"] is None


def test_compact_project_detail_uses_lightweight_metadata_sidecar(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    project_id = "temporal-tanger-city-mqnueqrr-llwf6o"
    project_dir = settings.temporal_projects_dir / project_id
    milestone_dir = project_dir / "milestones" / "WB_2026_R05"
    milestone_dir.mkdir(parents=True)
    project_json = project_dir / "project.json"
    project_json.write_text("{not parsed", encoding="utf-8")
    _write_project_summary(project_dir, project_id)
    _write_json(
        project_dir / "project_compact_metadata.json",
        {
            "project_id": project_id,
            "project_json_mtime_ns": project_json.stat().st_mtime_ns,
            "aoi_geojson": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
            },
            "milestones": [
                {
                    "release_identifier": "WB_2026_R05",
                    "release_date": "2026-05-28",
                    "status": "complete",
                    "source_mode": "automated",
                    "warnings": ["metadata warning"],
                    "error_message": None,
                    "metrics": {
                        "added_area_m2": 684677.97,
                        "total_area_m2": 2812701.98,
                        "additions_feature_count": 2623,
                        "effective_feature_count": 7391,
                        "building_level_available": True,
                        "added_block_count": 1229,
                        "cumulative_block_count": 3540,
                        "added_block_area_m2": 827021.51,
                        "cumulative_block_area_m2": 3419223.58,
                        "growth_envelope_area_m2": 91768057.87,
                    },
                },
            ],
        },
    )

    payload = service.load_temporal_project_compact_payload(project_id, settings)

    milestone = payload["milestones"][0]
    assert payload["aoi_geojson"]["coordinates"] == [[[0, 0], [1, 0], [1, 1], [0, 0]]]
    assert milestone["release_date"] == "2026-05-28"
    assert milestone["warnings"] == ["metadata warning"]
    assert milestone["metrics"]["added_area_m2"] == 684677.97


def test_compact_project_detail_complete_count_matches_metadata_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    project_id = "temporal-casa-mixed-status"
    project_dir = settings.temporal_projects_dir / project_id
    project_json = project_dir / "project.json"
    project_json.parent.mkdir(parents=True)
    project_json.write_text("{not parsed", encoding="utf-8")
    _write_project_summary(project_dir, project_id)
    monkeypatch.setattr(
        service,
        "_compact_reference_cog_bounds_wgs84",
        lambda path: [-7.7, 33.4, -7.5, 33.6],
    )

    baseline_dir = project_dir / "milestones" / "WB_2024_R02"
    result_dir = project_dir / "milestones" / "WB_2025_R03"
    future_dir = project_dir / "milestones" / "WB_2026_R05"
    baseline_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    future_dir.mkdir(parents=True)
    (baseline_dir / "reference_imagery_cog.tif").write_bytes(b"fake-cog")
    _write_geojson(result_dir / "additions.geojson", 1)
    _write_json(
        project_dir / "project_compact_metadata.json",
        {
            "project_id": project_id,
            "project_json_mtime_ns": project_json.stat().st_mtime_ns,
            "milestones": [
                {"release_identifier": "WB_2024_R02", "status": "pending", "metrics": None},
                {
                    "release_identifier": "WB_2025_R03",
                    "status": "complete",
                    "metrics": {"added_area_m2": 12.0, "additions_feature_count": 1},
                },
                {"release_identifier": "WB_2026_R05", "status": "pending", "metrics": None},
            ],
        },
    )

    payload = service.load_temporal_project_compact_payload(project_id, settings)

    statuses = {milestone["release_identifier"]: milestone["status"] for milestone in payload["milestones"]}
    assert statuses == {
        "WB_2024_R02": "pending",
        "WB_2025_R03": "complete",
        "WB_2026_R05": "pending",
    }
    assert payload["complete_milestone_count"] == 1
    assert payload["status"] == "pending"
