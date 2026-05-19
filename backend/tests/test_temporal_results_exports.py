from __future__ import annotations

import json
from pathlib import Path
import re
import zipfile

from fastapi.testclient import TestClient

from src.api.deps import get_app_settings
from src.api.main import app
from src.config import Settings
from src.schemas import TemporalMilestone, TemporalMilestoneMetrics, TemporalProject
from src.services.temporal_exports import build_temporal_results_export_file
from src.services.temporal_projects import save_temporal_project


def _feature_collection() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "area_m2": 123.4,
                    "score": 0.91,
                    "run_id": "debug-run",
                    "release_identifier": "WB_DEBUG",
                    "source_backend": "debug-backend",
                    "feature_index": 99,
                    "confidence": 0.91,
                    "status": "accepted",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-7.0, 33.0],
                        [-6.999, 33.0],
                        [-6.999, 33.001],
                        [-7.0, 33.001],
                        [-7.0, 33.0],
                    ]],
                },
            }
        ],
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(runtime_cache_dir=tmp_path)


def _project() -> TemporalProject:
    fc = _feature_collection()
    return TemporalProject(
        project_id="temporal-export-formats-test",
        name="Export Formats",
        aoi_geojson=fc["features"][0]["geometry"],
        milestones=[
            TemporalMilestone(
                release_identifier="WB_2022_R03",
                release_date="2022-03-16",
                status="complete",
                metrics=TemporalMilestoneMetrics(total_area_m2=1000.0),
            ),
            TemporalMilestone(
                release_identifier="WB_2024_R03",
                release_date="2024-03-28",
                status="complete",
                pair_request_hash="run-2024",
                additions_geojson=fc,
                cumulative_growth_blocks_geojson=fc,
                cumulative_growth_envelope_geojson=fc,
                automated_candidate_footprint_geojson=fc,
                buffer_layers_geojson={"10m": fc, "15m": fc, "20m": fc},
                metrics=TemporalMilestoneMetrics(
                    added_area_m2=123.4,
                    total_area_m2=1123.4,
                    additions_feature_count=1,
                    added_block_count=1,
                    cumulative_block_area_m2=123.4,
                ),
            ),
        ],
        created_at="2026-05-19T00:00:00Z",
        updated_at="2026-05-19T00:00:00Z",
    )


def _save_project(tmp_path: Path) -> Settings:
    settings = _settings(tmp_path)
    save_temporal_project(_project(), settings)
    return settings


def test_temporal_results_export_formats_are_generated_under_project_exports(tmp_path: Path) -> None:
    settings = _save_project(tmp_path)
    project_id = "temporal-export-formats-test"

    paths = {
        export_format: build_temporal_results_export_file(project_id, export_format, settings=settings)
        for export_format in ("xlsx", "kml", "geojson", "topojson", "json", "tsv", "shapefile")
    }

    assert all(path.parent == settings.temporal_projects_dir / project_id / "exports" for path in paths.values())
    assert paths["xlsx"].name == "results.xlsx"
    assert paths["kml"].read_bytes().startswith(b"<?xml")

    geojson = json.loads(paths["geojson"].read_text())
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"]
    first_props = geojson["features"][0]["properties"]
    assert first_props["project_id"] == project_id
    assert first_props["release_identifier"] == "WB_2024_R03"
    assert first_props["layer_type"] in {"additions", "cumulative_growth", "buffer_10m", "buffer_15m", "buffer_20m", "diagnostics"}
    assert "area_m2" in first_props

    summary = json.loads(paths["json"].read_text())
    assert summary["project"]["project_id"] == project_id
    assert "features" not in json.dumps(summary)

    tsv = paths["tsv"].read_text()
    assert tsv.splitlines()[0].split("\t")[:6] == [
        "project_id",
        "project_name",
        "run_id",
        "release_identifier",
        "date",
        "layer_type",
    ]
    assert "\"geometry\"" not in tsv

    topojson = json.loads(paths["topojson"].read_text())
    assert topojson["type"] == "Topology"
    assert "results" in topojson["objects"]
    assert topojson["bbox"]
    assert topojson["transform"]
    assert topojson["arcs"]
    assert not (paths["topojson"].parent / "results_full.topojson").exists()

    with zipfile.ZipFile(paths["shapefile"]) as archive:
        names = archive.namelist()
        assert any(name.endswith(".shp") for name in names)
        assert any(name.endswith(".shx") for name in names)
        assert any(name.endswith(".dbf") for name in names)
        assert any(name.endswith(".prj") for name in names)


def test_temporal_results_export_reuses_valid_cache(tmp_path: Path) -> None:
    settings = _save_project(tmp_path)
    first = build_temporal_results_export_file("temporal-export-formats-test", "geojson", settings=settings)
    first_mtime = first.stat().st_mtime_ns
    second = build_temporal_results_export_file("temporal-export-formats-test", "geojson", settings=settings)
    assert second == first
    assert second.stat().st_mtime_ns == first_mtime


def test_temporal_results_topojson_is_clean_quantized_default(tmp_path: Path) -> None:
    settings = _save_project(tmp_path)
    path = build_temporal_results_export_file("temporal-export-formats-test", "topojson", settings=settings)
    payload = json.loads(path.read_text())

    assert payload["type"] == "Topology"
    assert isinstance(payload["bbox"], list)
    assert len(payload["bbox"]) == 4
    assert set(payload["transform"]) == {"scale", "translate"}
    assert payload["objects"]["results"]["type"] == "GeometryCollection"
    assert payload["arcs"]

    def assert_integer_arcs(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                assert_integer_arcs(item)
        else:
            assert isinstance(value, int)

    assert_integer_arcs(payload["arcs"])

    geometries = payload["objects"]["results"]["geometries"]
    assert geometries
    allowed_property_keys = {"id", "project", "date", "year", "period", "layer", "area_m2", "area_ha"}
    removed_keys = {
        "run_id",
        "release_identifier",
        "release_t1",
        "release_t2",
        "src_date_t1",
        "src_date_t2",
        "source_backend",
        "feature_index",
        "buffer_id",
        "buffer_part_index",
        "source_change_block_id",
        "source_change_count",
        "block_gap_m",
        "cluster_gap_m",
        "kind",
        "release_date",
        "source_building_count",
        "confidence",
        "status",
        "score",
    }
    layers = {geometry["properties"]["layer"] for geometry in geometries}
    assert layers == {"additions", "buffer_10m", "cumulative_growth"}
    assert "diagnostics" not in layers
    assert "buffer_15m" not in layers
    assert "buffer_20m" not in layers

    for geometry in geometries:
        properties = geometry["properties"]
        assert set(properties) == allowed_property_keys
        assert not (set(properties) & removed_keys)
        assert re.fullmatch(r"^[0-9]{4}-[a-z0-9-]+-[0-9]{6}$", properties["id"])
        assert properties["project"] == "Export Formats"
        assert properties["date"] == "2024-03-28"
        assert properties["year"] == 2024
        assert properties["layer"] in {"additions", "buffer_10m", "cumulative_growth"}
        assert properties["area_ha"] == round(properties["area_m2"] / 10000, 4)

    metadata_path = path.with_name("results.topojson.metadata.json")
    assert metadata_path.is_file()
    assert json.loads(metadata_path.read_text())["version"] == "clean-quantized-v3"


def test_temporal_results_export_route_headers_and_unsupported_format(tmp_path: Path) -> None:
    settings = _save_project(tmp_path)
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        client = TestClient(app)
        response = client.get("/api/temporal-projects/temporal-export-formats-test/exports/results.geojson")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/geo+json")
        assert "resultats_temporal-export-formats-test.geojson" in response.headers["content-disposition"]

        tsv = client.get("/api/temporal-projects/temporal-export-formats-test/exports/results.tsv")
        assert tsv.status_code == 200
        assert tsv.headers["content-type"].startswith("text/tab-separated-values")

        shapefile = client.get("/api/temporal-projects/temporal-export-formats-test/exports/results_shapefile.zip")
        assert shapefile.status_code == 200
        assert shapefile.headers["content-type"].startswith("application/zip")

        unsupported = client.get("/api/temporal-projects/temporal-export-formats-test/exports/results.parquet")
        assert unsupported.status_code == 400
        assert unsupported.json()["detail"]["code"] == "unsupported_export_format"

        full_topojson = client.get("/api/temporal-projects/temporal-export-formats-test/exports/results.topojson?mode=full")
        assert full_topojson.status_code == 400
        assert full_topojson.json()["detail"]["code"] == "unsupported_export_mode"
    finally:
        app.dependency_overrides.clear()
