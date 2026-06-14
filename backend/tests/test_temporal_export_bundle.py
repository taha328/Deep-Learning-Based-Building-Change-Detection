from __future__ import annotations

import csv
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import zipfile

import numpy as np
from osgeo import ogr
import rasterio
from rasterio.transform import from_origin
from fastapi.testclient import TestClient

from src.api.deps import get_app_settings
from src.api.main import app
from src.config import Settings
from src.schemas import TemporalMilestone, TemporalMilestoneMetrics, TemporalProject, TemporalReferenceImagery
from src.services.temporal_projects import create_temporal_project_bundle, save_temporal_project


def _write_fake_tif(path: Path) -> None:
    profile = {
        "driver": "GTiff",
        "height": 16,
        "width": 16,
        "count": 4,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(-7.0, 33.01, 0.0001, 0.0001),
        "tiled": True,
    }
    rgb = np.full((3, 16, 16), 120, dtype=np.uint8)
    alpha = np.full((1, 16, 16), 255, dtype=np.uint8)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.vstack([rgb, alpha]))
        dst.build_overviews([2, 4], rasterio.enums.Resampling.nearest)


def _write_pair_summary(path: Path, t1_identifier: str, t2_identifier: str, t2_src_date: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "label",
                "identifier",
                "zoom",
                "release_date",
                "provider",
                "source_type",
                "dominant_src_date",
                "dominant_src_res_m",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "label": "t1",
                "identifier": t1_identifier,
                "zoom": 19,
                "release_date": "2022-03-16",
                "provider": "esri_wayback",
                "source_type": "historical_release",
                "dominant_src_date": "2021-02-23",
                "dominant_src_res_m": 0.5,
            }
        )
        writer.writerow(
            {
                "label": "t2",
                "identifier": t2_identifier,
                "zoom": 19,
                "release_date": "2024-03-28",
                "provider": "esri_wayback",
                "source_type": "historical_release",
                "dominant_src_date": t2_src_date,
                "dominant_src_res_m": 0.46,
            }
        )


def _feature_collection() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "feat", "score": 1.2},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-7.0, 33.0], [-6.9998, 33.0], [-6.9998, 33.0002], [-7.0, 33.0002], [-7.0, 33.0]]],
                },
            }
        ],
    }


def _read_qgs_from_qgz(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as archive:
        qgs_members = [name for name in archive.namelist() if name.endswith(".qgs")]
        assert len(qgs_members) == 1
        return archive.read(qgs_members[0]).decode("utf-8")


def test_temporal_export_bundle_route_returns_controlled_error(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)

    def _fail_bundle(*args, **kwargs):
        raise ValueError("GeoPackage validation failed")

    monkeypatch.setattr("src.api.routes.temporal_projects.create_temporal_project_bundle", _fail_bundle)
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        response = TestClient(app, raise_server_exceptions=False).post("/api/temporal-projects/temporal-demo/export-bundle")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "temporal_qgis_export_failed"
    assert "GeoPackage validation failed" in response.json()["detail"]["message"]


def test_temporal_export_bundle_uses_qgz_and_one_gpkg_without_csv_geojson_or_readme(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    settings = Settings(runtime_cache_dir=tmp_path)
    pair_hash_2024 = "hash-2024"
    pair_hash_2026 = "hash-2026"
    pair_2024_dir = settings.request_cache_dir / pair_hash_2024
    pair_2026_dir = settings.request_cache_dir / pair_hash_2026
    pair_2024_dir.mkdir(parents=True, exist_ok=True)
    pair_2026_dir.mkdir(parents=True, exist_ok=True)

    _write_pair_summary(pair_2024_dir / "wayback_pair_summary.csv", "WB_2022_R03", "WB_2024_R03", "2023-02-09")
    _write_pair_summary(pair_2026_dir / "wayback_pair_summary.csv", "WB_2024_R03", "WB_2026_R03", "2025-02-01")
    (pair_2024_dir / "export_bundle.zip").write_bytes(b"nested")
    (pair_2026_dir / "export_bundle.zip").write_bytes(b"nested")

    tif_2022 = pair_2024_dir / "t1_wayback_rgb.tif"
    tif_2024 = pair_2024_dir / "t2_wayback_rgb.tif"
    tif_2026 = pair_2026_dir / "t2_wayback_rgb.tif"
    _write_fake_tif(tif_2022)
    _write_fake_tif(tif_2024)
    _write_fake_tif(tif_2026)

    project = TemporalProject(
        project_id="temporal-export-test",
        name="Marrakech",
        aoi_geojson=_feature_collection()["features"][0]["geometry"],
        milestones=[
            TemporalMilestone(
                release_identifier="WB_2022_R03",
                release_date="2022-03-16",
                status="complete",
                metrics=TemporalMilestoneMetrics(),
                reference_imagery=TemporalReferenceImagery(cog_path=str(tif_2022)),
            ),
            TemporalMilestone(
                release_identifier="WB_2024_R03",
                release_date="2024-03-28",
                status="complete",
                pair_request_hash=pair_hash_2024,
                additions_geojson=_feature_collection(),
                buffer_layers_geojson={"10m": _feature_collection(), "15m": _feature_collection(), "20m": _feature_collection()},
                cumulative_union_geojson=_feature_collection(),
                cumulative_convex_hull_geojson=_feature_collection(),
                cumulative_growth_envelope_geojson=_feature_collection(),
                metrics=TemporalMilestoneMetrics(added_area_m2=10.0, additions_feature_count=1, added_block_count=1),
                reference_imagery=TemporalReferenceImagery(cog_path=str(tif_2024)),
            ),
            TemporalMilestone(
                release_identifier="WB_2026_R03",
                release_date="2026-03-25",
                status="complete",
                pair_request_hash=pair_hash_2026,
                additions_geojson=_feature_collection(),
                buffer_layers_geojson={"10m": _feature_collection(), "15m": _feature_collection(), "20m": _feature_collection()},
                cumulative_union_geojson=_feature_collection(),
                cumulative_convex_hull_geojson=_feature_collection(),
                cumulative_growth_envelope_geojson=_feature_collection(),
                metrics=TemporalMilestoneMetrics(added_area_m2=8.0, additions_feature_count=1, added_block_count=1),
                reference_imagery=TemporalReferenceImagery(cog_path=str(tif_2026)),
            ),
        ],
        created_at="2026-05-07T00:00:00Z",
        updated_at="2026-05-07T00:00:00Z",
    )
    save_temporal_project(project, settings)

    bundle_path = create_temporal_project_bundle(project.project_id, settings=settings, force=True)
    assert "QGIS_GPKG_VALIDATE_INPUT" in caplog.text
    assert "QGIS_GPKG_LAYER_WRITE" in caplog.text
    assert "QGIS_GPKG_LAYER_SKIPPED_EMPTY" in caplog.text
    end_ym = "2025-02"
    assert bundle_path.name == f"Marrakech_2022-03_{end_ym}_export_QGIS.zip"

    with zipfile.ZipFile(bundle_path, "r") as archive:
        names = archive.namelist()
        assert any(name.endswith(f"/qgis/Marrakech_2022-03_{end_ym}.qgz") for name in names)
        assert any(name.endswith(f"/donnees/vecteurs/Marrakech_2022-03_{end_ym}.gpkg") for name in names)
        assert any(name.endswith("/manifeste_projet.json") for name in names)
        assert any("2023_02_WB_2024_R03_imagerie_de_reference.tif" in name for name in names)
        assert any("2025_02_WB_2026_R03_imagerie_de_reference.tif" in name for name in names)

        assert not any(name.endswith(".csv") for name in names)
        assert not any(name.endswith(".geojson") for name in names)
        assert not any(name.endswith("LISEZ_MOI.txt") for name in names)
        assert not any(name.endswith(".zip") and name != bundle_path.name for name in names)
        assert not any("manual_override" in name for name in names)
        assert not any("reference_labels" in name for name in names)
        assert not any("automated_candidate_footprint" in name for name in names)
        assert not any("effective_footprint" in name for name in names)
        assert not any("cumulative_growth_blocks" in name for name in names)
        assert not any("cumulative_growth_envelope.geojson" in name for name in names)
        assert not any("change_probability" in name for name in names)
        assert not any("building_change_labels" in name for name in names)

        qgz_member = next(name for name in names if name.endswith(".qgz"))
        gpkg_member = next(name for name in names if name.endswith(".gpkg"))
        manifest_member = next(name for name in names if name.endswith("manifeste_projet.json"))
        qgz_path = tmp_path / "project.qgz"
        gpkg_path = tmp_path / "layers.gpkg"
        qgz_path.write_bytes(archive.read(qgz_member))
        gpkg_path.write_bytes(archive.read(gpkg_member))

        qgs = _read_qgs_from_qgz(qgz_path)
        assert "../donnees/rasters/" in qgs
        assert "../donnees/vecteurs/" in qgs
        assert ".geojson" not in qgs
        assert ".csv" not in qgs
        assert "LISEZ_MOI" not in qgs
        assert "Tampon changement bâtiment 10 m" in qgs
        assert "Imagerie de référence - février 2025" in qgs

        datasource = ogr.Open(str(gpkg_path), 0)
        assert datasource is not None
        layer_names = {datasource.GetLayerByIndex(index).GetName() for index in range(datasource.GetLayerCount())}
        datasource = None
        assert "ajouts_2023_02" in layer_names
        assert "tampon_changement_batiment_10m_2023_02" in layer_names
        assert "union_cumulative_2023_02" not in layer_names
        assert "polygone_convexe_2023_02" not in layer_names
        assert "ajouts_2025_02" in layer_names

        manifest = json.loads(archive.read(manifest_member).decode("utf-8"))
        assert manifest["export_filename"] == bundle_path.name
        assert manifest["date_range"]["start"] == "2022-03"
        assert manifest["date_range"]["end"] == end_ym
        assert manifest["qgz_path"] == f"qgis/Marrakech_2022-03_{end_ym}.qgz"
        assert manifest["gpkg_path"] == f"donnees/vecteurs/Marrakech_2022-03_{end_ym}.gpkg"
        assert "ajouts_2023_02" in manifest["gpkg_layer_names"]

        raster_members = [name for name in names if "/donnees/rasters/" in name and name.endswith(".tif")]
        assert raster_members
        for index, member in enumerate(raster_members):
            raster_path = tmp_path / f"raster-{index}.tif"
            raster_path.write_bytes(archive.read(member))
            with rasterio.open(raster_path) as src:
                assert src.crs is not None
                assert src.transform is not None
