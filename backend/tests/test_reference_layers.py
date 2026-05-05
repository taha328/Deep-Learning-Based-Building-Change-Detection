from __future__ import annotations

import asyncio
import io
import json
import shutil
import zipfile
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fastapi import UploadFile
from osgeo import ogr, osr
from rasterio.transform import from_origin

from src.config import Settings
from src.schemas import ReferenceLayerPatchRequest, TemporalProject
from src.services import reference_layers as reference_layers_service
from src.services.reference_layers import (
    PmtilesToolStatus,
    ReferenceLayerError,
    delete_reference_layer,
    import_reference_layer,
    list_reference_layers,
    preflight_reference_layer,
    update_reference_layer,
)
from src.services.temporal_projects import save_temporal_project

ogr.UseExceptions()


def _settings(tmp_path: Path, **overrides) -> Settings:
    params = {
        "runtime_cache_dir": tmp_path,
        "reference_layer_browser_geojson_max_features": 25_000,
    }
    params.update(overrides)
    return Settings(**params)


def _project(tmp_path: Path) -> TemporalProject:
    return TemporalProject(
        project_id="reference-layer-test",
        name="Reference Layer Test",
        project_dir=str(tmp_path / "temporal_projects" / "reference-layer-test"),
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[
                [-7.0, 33.0],
                [-6.99, 33.0],
                [-6.99, 33.01],
                [-7.0, 33.01],
                [-7.0, 33.0],
            ]],
        },
        milestones=[],
        created_at="2026-05-05T00:00:00Z",
        updated_at="2026-05-05T00:00:00Z",
    )


def _geojson_bytes(coordinates: list[list[float]] | None = None) -> bytes:
    coords = coordinates or [[-7.001, 33.005], [-6.995, 33.005]]
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "road"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                }
            ],
        }
    ).encode("utf-8")


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _save_project(settings: Settings, tmp_path: Path) -> TemporalProject:
    return save_temporal_project(_project(tmp_path), settings)


def run_async(coro):
    return asyncio.run(coro)


def _epsg4326() -> osr.SpatialReference:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    return srs


def _create_vector_dataset(path: Path, driver_name: str, geometry_type: int, wkt_geometries: list[str], layer_name: str = "layer") -> Path:
    driver = ogr.GetDriverByName(driver_name)
    if driver is None:
        pytest.skip(f"{driver_name} driver unavailable")
    if path.exists():
        driver.DeleteDataSource(str(path))
    datasource = driver.CreateDataSource(str(path))
    if datasource is None:
        pytest.skip(f"{driver_name} datasource creation unavailable")
    layer = datasource.CreateLayer(layer_name, _epsg4326(), geom_type=geometry_type)
    layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    for index, wkt_geometry in enumerate(wkt_geometries):
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("name", f"feature-{index}")
        feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt_geometry))
        layer.CreateFeature(feature)
        feature = None
    datasource = None
    return path


def _zip_paths(zip_path: Path, files: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for file_path in files:
            archive.write(file_path, arcname=file_path.name)
    return buffer.getvalue()


def _create_shapefile_zip(tmp_path: Path, *, include_prj: bool = True, multiple_layers: bool = False) -> bytes:
    work_dir = tmp_path / f"shapefile_{'multi' if multiple_layers else 'single'}"
    work_dir.mkdir(parents=True, exist_ok=True)
    first = _create_vector_dataset(
        work_dir / "roads.shp",
        "ESRI Shapefile",
        ogr.wkbLineString,
        ["LINESTRING (-7 33.001, -6.995 33.005)"],
    )
    files = list(work_dir.glob("roads.*"))
    if not include_prj:
        files = [file for file in files if file.suffix.lower() != ".prj"]
    if multiple_layers:
        second = _create_vector_dataset(
            work_dir / "buildings.shp",
            "ESRI Shapefile",
            ogr.wkbPolygon,
            ["POLYGON ((-7 33, -6.998 33, -6.998 33.002, -7 33.002, -7 33))"],
        )
        del second
        files.extend(work_dir.glob("buildings.*"))
    return _zip_paths(tmp_path / "layer.zip", files)


def _create_gpkg_bytes(tmp_path: Path) -> bytes:
    gpkg_path = _create_vector_dataset(
        tmp_path / "roads.gpkg",
        "GPKG",
        ogr.wkbLineString,
        ["LINESTRING (-7 33.001, -6.995 33.005)"],
    )
    return gpkg_path.read_bytes()


def _create_kml_bytes(tmp_path: Path) -> bytes:
    kml_path = _create_vector_dataset(
        tmp_path / "roads.kml",
        "KML",
        ogr.wkbLineString,
        ["LINESTRING (-7 33.001, -6.995 33.005)"],
    )
    return kml_path.read_bytes()


def _create_kmz_bytes(tmp_path: Path) -> bytes:
    kml_path = _create_vector_dataset(
        tmp_path / "roads.kml",
        "KML",
        ogr.wkbLineString,
        ["LINESTRING (-7 33.001, -6.995 33.005)"],
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.write(kml_path, arcname="doc.kml")
    return buffer.getvalue()


def _create_gpx_bytes(tmp_path: Path) -> bytes:
    driver = ogr.GetDriverByName("GPX")
    if driver is None:
        pytest.skip("GPX driver unavailable")
    gpx_path = tmp_path / "tracks.gpx"
    if gpx_path.exists():
        driver.DeleteDataSource(str(gpx_path))
    datasource = driver.CreateDataSource(str(gpx_path))
    if datasource is None:
        pytest.skip("GPX datasource creation unavailable")
    layer = datasource.CreateLayer("tracks", _epsg4326(), geom_type=ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetField("name", "track-1")
    feature.SetGeometry(ogr.CreateGeometryFromWkt("LINESTRING (-7 33.001, -6.995 33.005)"))
    layer.CreateFeature(feature)
    feature = None
    datasource = None
    return gpx_path.read_bytes()


def _available_pmtiles_status(tmp_path: Path) -> PmtilesToolStatus:
    return PmtilesToolStatus(
        available=True,
        tippecanoe_available=True,
        pmtiles_available=True,
        tippecanoe_path=str(tmp_path / "bin" / "tippecanoe"),
        pmtiles_path=str(tmp_path / "bin" / "pmtiles"),
        reason=None,
    )


def _mock_pmtiles_builder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(reference_layers_service, "_pmtiles_tool_status", lambda settings: _available_pmtiles_status(tmp_path))

    def _builder(*, normalized_geojson_path: Path, layer_dir: Path, source_layer: str, settings: Settings):
        assert normalized_geojson_path.exists()
        build_dir = layer_dir / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "layer.mbtiles").write_bytes(b"temporary-mbtiles")
        output_path = layer_dir / "display" / "layer.pmtiles"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"pmtiles-data")
        if not settings.reference_layer_pmtiles_keep_intermediate:
            shutil.rmtree(build_dir, ignore_errors=True)
        return output_path, [f"layer={source_layer}"]

    monkeypatch.setattr(reference_layers_service, "_build_pmtiles_artifact", _builder)


def test_reference_layer_preflight_accepts_geojson(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings))
    assert result.original_format == "geojson"
    assert result.layer_kind == "vector"
    assert result.geometry_type == "line"


def test_reference_layer_preflight_accepts_json_geojson(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.json", _geojson_bytes()), settings=settings))
    assert result.original_format == "geojson"


def test_reference_layer_preflight_accepts_gpkg_if_driver_available(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.gpkg", _create_gpkg_bytes(tmp_path)), settings=settings))
    assert result.original_format == "gpkg"
    assert result.geometry_type == "line"


def test_reference_layer_preflight_accepts_zipped_shapefile(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.zip", _create_shapefile_zip(tmp_path)), settings=settings))
    assert result.original_format == "shapefile_zip"
    assert result.geometry_type == "line"


def test_reference_layer_preflight_accepts_shz_or_skips_if_driver_unavailable(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.shz", _create_shapefile_zip(tmp_path)), settings=settings))
    assert result.original_format == "shz"


def test_reference_layer_preflight_accepts_kml_if_driver_available(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.kml", _create_kml_bytes(tmp_path)), settings=settings))
    assert result.original_format == "kml"


def test_reference_layer_preflight_accepts_kmz_if_driver_available(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.kmz", _create_kmz_bytes(tmp_path)), settings=settings))
    assert result.original_format == "kmz"


def test_reference_layer_preflight_accepts_gpx_if_driver_available(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    result = run_async(preflight_reference_layer(project.project_id, _upload("tracks.gpx", _create_gpx_bytes(tmp_path)), settings=settings))
    assert result.original_format == "gpx"


def test_reference_layer_raster_preflight_accepts_geotiff(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    raster_path = tmp_path / "reference.tif"
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(-7.0, 33.01, 0.001, 0.001),
    ) as dataset:
        dataset.write(np.ones((4, 4), dtype=np.uint8), 1)

    result = run_async(preflight_reference_layer(project.project_id, _upload("raster.tif", raster_path.read_bytes()), settings=settings))
    assert result.layer_kind == "raster"
    assert result.original_format == "geotiff"


def test_reference_layer_rejects_unsupported_format(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(preflight_reference_layer(project.project_id, _upload("layer.txt", b"hello"), settings=settings))
    assert exc_info.value.code == "unsupported_format"


def test_reference_layer_zip_rejects_zip_slip(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../evil.shp", b"bad")
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(preflight_reference_layer(project.project_id, _upload("layer.zip", buffer.getvalue()), settings=settings))
    assert exc_info.value.code == "unsafe_archive"


def test_reference_layer_zip_rejects_absolute_paths(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("/evil.shp", b"bad")
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(preflight_reference_layer(project.project_id, _upload("layer.zip", buffer.getvalue()), settings=settings))
    assert exc_info.value.code == "unsafe_archive"


def test_reference_layer_zip_rejects_missing_required_shapefile_sidecars(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(preflight_reference_layer(project.project_id, _upload("roads.zip", _create_shapefile_zip(tmp_path, include_prj=False)), settings=settings))
    assert exc_info.value.code == "missing_required_sidecar"


def test_reference_layer_zip_rejects_multiple_shp_layers_without_layer_selection(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(preflight_reference_layer(project.project_id, _upload("roads.zip", _create_shapefile_zip(tmp_path, multiple_layers=True)), settings=settings))
    assert exc_info.value.code == "multiple_layers_not_supported"


def test_reference_layer_zip_rejects_missing_crs_when_required(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(preflight_reference_layer(project.project_id, _upload("roads.zip", _create_shapefile_zip(tmp_path, include_prj=False)), settings=settings))
    assert exc_info.value.code == "missing_required_sidecar"


def test_reference_layer_metadata_save_creates_parent_directory(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    layer = run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    metadata_path = Path(project.project_dir) / "reference_layers" / "reference_layers.json"
    assert metadata_path.exists()
    assert layer.display_url is not None


def test_reference_layer_artifact_dirs_created_before_write(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    layer_root = Path(project.project_dir) / "reference_layers"
    layer_dir = next(path for path in layer_root.iterdir() if path.is_dir())
    assert (layer_dir / "original").exists()
    assert (layer_dir / "display").exists()


def test_reference_layer_polygon_and_multipolygon_classified_as_polygon(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[-7, 33], [-6.999, 33], [-6.999, 33.001], [-7, 33.001], [-7, 33]]]}},
                {"type": "Feature", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": [[[[-6.998, 33], [-6.997, 33], [-6.997, 33.001], [-6.998, 33.001], [-6.998, 33]]]]}},
            ],
        }
    ).encode("utf-8")
    result = run_async(preflight_reference_layer(project.project_id, _upload("polygons.geojson", payload), settings=settings))
    assert result.geometry_type == "polygon"


def test_reference_layer_line_and_multiline_classified_as_line(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[-7, 33], [-6.999, 33.001]]}},
                {"type": "Feature", "properties": {}, "geometry": {"type": "MultiLineString", "coordinates": [[[-6.998, 33], [-6.997, 33.001]]]}},
            ],
        }
    ).encode("utf-8")
    result = run_async(preflight_reference_layer(project.project_id, _upload("lines.geojson", payload), settings=settings))
    assert result.geometry_type == "line"


def test_reference_layer_point_and_multipoint_classified_as_point(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [-7, 33]}},
                {"type": "Feature", "properties": {}, "geometry": {"type": "MultiPoint", "coordinates": [[-6.999, 33], [-6.998, 33.001]]}},
            ],
        }
    ).encode("utf-8")
    result = run_async(preflight_reference_layer(project.project_id, _upload("points.geojson", payload), settings=settings))
    assert result.geometry_type == "point"


def test_reference_layer_empty_aoi_clip_returns_warning_and_not_visible(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    outside = _geojson_bytes([[-8.0, 34.0], [-7.9, 34.1]])
    layer = run_async(import_reference_layer(project.project_id, _upload("outside.geojson", outside), settings=settings, name="Outside"))
    assert layer.visible is False
    assert any("no features intersect" in warning.lower() for warning in layer.warnings)


def test_reference_layer_large_clipped_geojson_blocked_or_requires_tiling(tmp_path) -> None:
    settings = _settings(tmp_path, reference_layer_browser_geojson_max_features=0)
    project = _save_project(settings, tmp_path)
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    assert exc_info.value.code == "display_geojson_too_large"


def test_reference_layer_full_layer_without_pmtiles_blocked(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    monkeypatch.setattr(
        reference_layers_service,
        "_pmtiles_tool_status",
        lambda settings: PmtilesToolStatus(False, False, False, None, None, "PMTiles support is disabled by configuration."),
    )
    with pytest.raises(ReferenceLayerError) as exc_info:
        run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads", scope="full_layer"))
    assert exc_info.value.code == "reference_layer_not_importable"


def test_reference_layer_does_not_return_raw_full_geojson(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    _mock_pmtiles_builder(monkeypatch, tmp_path)
    layer = run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads", scope="full_layer"))
    assert layer.storage_strategy == "pmtiles"
    assert layer.display_path is None
    assert layer.pmtiles_url is not None


def test_reference_layer_safe_display_url_returned(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    layer = run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    assert layer.display_url is not None
    assert layer.display_url.startswith("/api/files?path=")
    assert "%2F" in layer.display_url


def test_reference_layer_import_does_not_modify_temporal_milestones(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    before = project.model_dump(mode="json")["milestones"]
    run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    reloaded = save_temporal_project(project, settings)
    assert reloaded.model_dump(mode="json")["milestones"] == before


def test_reference_layer_import_does_not_modify_manual_override(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _project(tmp_path)
    saved = save_temporal_project(project, settings)
    run_async(import_reference_layer(saved.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    assert saved.milestones == []


def test_full_layer_vector_preflight_selects_pmtiles(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    monkeypatch.setattr(reference_layers_service, "_pmtiles_tool_status", lambda settings: _available_pmtiles_status(tmp_path))
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, scope="full_layer"))
    assert result.storage_strategy == "pmtiles"
    assert result.tool_status["pmtiles"] == "available"


def test_full_layer_vector_preflight_reports_missing_tippecanoe(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    monkeypatch.setattr(
        reference_layers_service,
        "_pmtiles_tool_status",
        lambda settings: PmtilesToolStatus(False, False, True, None, "/tmp/pmtiles", "Tippecanoe is not available on this backend."),
    )
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, scope="full_layer"))
    assert any("Tippecanoe" in item for item in result.errors)


def test_full_layer_vector_preflight_reports_missing_pmtiles_cli_when_conversion_required(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    monkeypatch.setattr(
        reference_layers_service,
        "_pmtiles_tool_status",
        lambda settings: PmtilesToolStatus(False, True, False, "/tmp/tippecanoe", None, "The pmtiles CLI is required to convert internal MBTiles artifacts."),
    )
    result = run_async(preflight_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, scope="full_layer"))
    assert any("pmtiles CLI" in item for item in result.errors)


def test_full_layer_vector_import_creates_pmtiles_artifact(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    _mock_pmtiles_builder(monkeypatch, tmp_path)
    layer = run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Road Network", scope="full_layer"))
    metadata = json.loads((Path(project.project_dir) / "reference_layers" / "reference_layers.json").read_text())
    display_path = Path(metadata[0]["display_path"])
    assert display_path.name == "layer.pmtiles"
    assert display_path.exists()
    assert layer.storage_strategy == "pmtiles"


def test_reference_layer_delete_removes_metadata_and_artifacts(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    layer = run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    delete_reference_layer(project.project_id, layer.layer_id, settings)
    assert list_reference_layers(project.project_id, settings) == []
    assert not (Path(project.project_dir) / "reference_layers" / layer.layer_id).exists()


def test_reference_layer_patch_updates_visibility_and_opacity(tmp_path) -> None:
    settings = _settings(tmp_path)
    project = _save_project(settings, tmp_path)
    layer = run_async(import_reference_layer(project.project_id, _upload("roads.geojson", _geojson_bytes()), settings=settings, name="Roads"))
    updated = update_reference_layer(project.project_id, layer.layer_id, ReferenceLayerPatchRequest(visible=False, opacity=0.25), settings)
    assert updated.visible is False
    assert updated.opacity == 0.25
