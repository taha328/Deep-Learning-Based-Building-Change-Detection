from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import warnings

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
from pydantic import ValidationError
import pytest
import rasterio
from affine import Affine
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import from_origin

from src.api.deps import get_app_settings
from src.api.main import app
from src.config import Settings
from src.domain.tiling import tile_range_for_bbox
from src.schemas import TemporalProject, TemporalReferenceImagery
from src.services.temporal_projects import get_temporal_project, save_temporal_project
from src.utils.geometry import bounds_dict, parse_aoi_geometry
import src.services.temporal_reference_imagery as reference_imagery_service
from src.services.temporal_reference_imagery import (
    TemporalReferenceSource,
    build_temporal_reference_imagery,
    build_reference_tilejson_payload_cached,
    clear_reference_tilejson_cache,
    clear_reference_tile_cache,
    ensure_reference_imagery_cog,
    reference_imagery_version_token,
    reference_tile_cache_path,
    render_reference_tile_png_cached,
    render_reference_tile_png,
    resolve_temporal_reference_cog_cached,
    resolve_temporal_reference_cog,
)


def _write_rgb_raster(path: Path) -> None:
    data = np.zeros((3, 512, 512), dtype=np.uint8)
    data[0, :, :] = 210
    data[1, 64:448, 64:448] = 140
    data[2, 128:384, 128:384] = 80
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=512,
        height=512,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(-1000.0, 1000.0, 10.0, 10.0),
    ) as dst:
        dst.write(data)


def _write_ungeoreferenced_rgb_raster(path: Path) -> None:
    data = np.zeros((3, 64, 64), dtype=np.uint8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=64,
        height=64,
        count=3,
        dtype="uint8",
    ) as dst:
        dst.write(data)


def _write_valid_mask_raster(path: Path, *, width: int = 512, height: int = 512) -> None:
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[64:448, 64:448] = 255
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(-1000.0, 1000.0, 10.0, 10.0),
    ) as dst:
        dst.write(mask, 1)


def _write_temporal_project_with_reference(
    settings: Settings,
    project_id: str,
    *,
    selected_release: str = "WB_2024_R02",
    include_tile_metadata: bool = True,
) -> Path:
    project_dir = settings.temporal_projects_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    cog_path = project_dir / "milestones" / selected_release / "reference_imagery_cog.tif"
    cog_path.parent.mkdir(parents=True, exist_ok=True)
    _write_rgb_raster(cog_path)
    project = TemporalProject(
        project_id=project_id,
        name="Temporal reference test",
        project_dir=str(project_dir),
        aoi_geojson=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        milestones=[
            {
                "release_identifier": "WB_2018_R04",
                "release_date": "2018-03-28",
            },
            {
                "release_identifier": selected_release,
                "release_date": "2024-06-01",
                "reference_imagery": (
                    TemporalReferenceImagery(
                        raster_bounds_wgs84=[-7.0, 33.0, -6.99, 33.01],
                        storage_strategy="raster_tiles",
                        cog_path=str(cog_path),
                        tilejson_url=f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json",
                        tiles_url_template=f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tiles/{{z}}/{{x}}/{{y}}.png",
                        minzoom=0,
                        maxzoom=18,
                        tile_size=256,
                    )
                    if include_tile_metadata
                    else TemporalReferenceImagery(
                        image_path=str(project_dir / "legacy_preview.png"),
                        raster_bounds_wgs84=[-7.0, 33.0, -6.99, 33.01],
                    )
                ),
            },
            {
                "release_identifier": "WB_2025_R03",
                "release_date": "2025-03-27",
            },
        ],
    )
    (project_dir / "project.json").write_text(json.dumps(project.model_dump(mode="json"), indent=2))
    return cog_path


def _test_aoi(offset: float = 0.0) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [-7.0000 + offset, 33.0000 + offset],
                [-6.9900 + offset, 33.0000 + offset],
                [-6.9900 + offset, 33.0100 + offset],
                [-7.0000 + offset, 33.0100 + offset],
                [-7.0000 + offset, 33.0000 + offset],
            ]
        ],
    }


def _write_temporal_project_without_reference(
    settings: Settings,
    project_id: str,
    *,
    selected_release: str = "WB_2024_R02",
    aoi_geojson: dict | None = None,
) -> Path:
    project_dir = settings.temporal_projects_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    project = TemporalProject(
        project_id=project_id,
        name="Temporal reference missing",
        project_dir=str(project_dir),
        aoi_geojson=aoi_geojson,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        milestones=[
            {
                "release_identifier": selected_release,
                "release_date": "2024-06-01",
                "additions_geojson": {"type": "FeatureCollection", "features": []},
            },
        ],
    )
    (project_dir / "project.json").write_text(json.dumps(project.model_dump(mode="json"), indent=2))
    return project_dir


def _write_reference_layers_metadata(project_dir: Path, *, count: int = 1) -> None:
    reference_layers_dir = project_dir / "reference_layers"
    reference_layers_dir.mkdir(parents=True, exist_ok=True)
    layers = []
    for index in range(count):
        layers.append(
            {
                "layer_id": f"layer-{index + 1}",
                "project_id": project_dir.name,
                "name": f"Reference Layer {index + 1}",
                "original_filename": f"layer-{index + 1}.geojson",
                "original_format": "geojson",
                "layer_kind": "vector",
                "geometry_type": "polygon",
                "scope": "aoi_clipped",
                "storage_strategy": "geojson",
                "crs": "EPSG:4326",
                "bounds_wgs84": [-7.0, 33.0, -6.99, 33.01],
                "feature_count": 1,
                "file_size_bytes": 128,
                "source_path": str(reference_layers_dir / f"layer-{index + 1}" / "source.geojson"),
                "display_path": str(reference_layers_dir / f"layer-{index + 1}" / "display.geojson"),
                "display_url": None,
                "pmtiles_url": None,
                "tilejson_url": None,
                "tiles_url_template": None,
                "source_layer": None,
                "style": {
                    "color": "#0ea5e9",
                    "line_width": 2.0,
                    "fill_color": "#0ea5e9",
                    "fill_opacity": 0.25,
                    "outline_color": "#0369a1",
                    "point_radius": 5.0,
                },
                "visible": True,
                "opacity": 0.85,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "warnings": [],
            }
        )
    (reference_layers_dir / "reference_layers.json").write_text(json.dumps(layers, indent=2))


def _client_with_settings(settings: Settings) -> TestClient:
    app.dependency_overrides[get_app_settings] = lambda: settings
    return TestClient(app)


def _clear_dependency_overrides() -> None:
    app.dependency_overrides.clear()


def test_build_temporal_reference_imagery_prefers_raster_tiles_when_source_raster_exists(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    source_raster_path = tmp_path / "source.tif"
    _write_rgb_raster(source_raster_path)

    imagery = build_temporal_reference_imagery(
        project_id="temporal-demo",
        project_dir=project_dir,
        release_identifier="WB_2024_R01",
        source=TemporalReferenceSource(
            image_path=str(tmp_path / "preview.png"),
            image_png_data_url="data:image/png;base64,legacy",
            raster_bounds_wgs84=None,
            source_raster_path=str(source_raster_path),
        ),
    )

    assert imagery is not None
    assert imagery.storage_strategy == "raster_tiles"
    assert imagery.image_path == str(tmp_path / "preview.png")
    assert imagery.image_png_data_url is None
    assert imagery.cog_path is not None
    assert Path(imagery.cog_path).is_file()
    assert imagery.tilejson_url == "/api/temporal-projects/temporal-demo/milestones/WB_2024_R01/reference/tilejson.json"
    assert imagery.tiles_url_template == "/api/temporal-projects/temporal-demo/milestones/WB_2024_R01/reference/tiles/{z}/{x}/{y}.png"
    assert imagery.raster_bounds_wgs84 is not None
    with rasterio.open(imagery.cog_path) as src:
        assert src.crs is not None
        assert src.transform != Affine.identity()
        assert src.profile.get("tiled") is True
        assert src.overviews(1)


def test_build_temporal_reference_imagery_preserves_image_overlay_fallback_without_source_raster(tmp_path: Path) -> None:
    imagery = build_temporal_reference_imagery(
        project_id="temporal-demo",
        project_dir=tmp_path / "project",
        release_identifier="WB_2024_R01",
        source=TemporalReferenceSource(
            image_path=str(tmp_path / "preview.png"),
            image_png_data_url="data:image/png;base64,legacy",
            raster_bounds_wgs84=[-7.0, 33.0, -6.9, 33.1],
            source_raster_path=None,
        ),
    )

    assert imagery is not None
    assert imagery.storage_strategy == "image_overlay"
    assert imagery.cog_path is None
    assert imagery.tilejson_url is None
    assert imagery.tiles_url_template is None
    assert imagery.image_path == str(tmp_path / "preview.png")


def test_render_reference_tile_png_returns_png_bytes(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    source_raster_path = tmp_path / "source.tif"
    _write_rgb_raster(source_raster_path)
    imagery = build_temporal_reference_imagery(
        project_id="temporal-demo",
        project_dir=project_dir,
        release_identifier="WB_2024_R01",
        source=TemporalReferenceSource(
            image_path=None,
            image_png_data_url=None,
            raster_bounds_wgs84=None,
            source_raster_path=str(source_raster_path),
        ),
    )

    cog_info = resolve_temporal_reference_cog(imagery)
    assert cog_info is not None

    tile_bytes = render_reference_tile_png(cog_info.cog_path, 0, 0, 0, tile_size=cog_info.tile_size)
    rendered = Image.open(BytesIO(tile_bytes))

    assert rendered.format == "PNG"
    assert rendered.size == (256, 256)


def test_render_reference_tile_png_cached_hits_process_cache(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source.tif"
    _write_rgb_raster(source_raster_path)
    imagery = build_temporal_reference_imagery(
        project_id="temporal-demo",
        project_dir=tmp_path / "project",
        release_identifier="WB_2024_R01",
        source=TemporalReferenceSource(
            image_path=None,
            image_png_data_url=None,
            raster_bounds_wgs84=None,
            source_raster_path=str(source_raster_path),
        ),
    )
    cog_info = resolve_temporal_reference_cog(imagery)
    assert cog_info is not None

    clear_reference_tile_cache()
    first = render_reference_tile_png_cached(
        project_id="temporal-demo",
        release_identifier="WB_2024_R01",
        cog_info=cog_info,
        z=0,
        x=0,
        y=0,
    )
    second = render_reference_tile_png_cached(
        project_id="temporal-demo",
        release_identifier="WB_2024_R01",
        cog_info=cog_info,
        z=0,
        x=0,
        y=0,
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.content == second.content


def test_ensure_reference_imagery_cog_reuses_existing_file_without_rewrite(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source.tif"
    cog_path = tmp_path / "cog" / "reference_imagery_cog.tif"
    _write_rgb_raster(source_raster_path)

    first_cog = ensure_reference_imagery_cog(source_raster_path, cog_path)
    first_mtime = first_cog.stat().st_mtime

    second_cog = ensure_reference_imagery_cog(source_raster_path, cog_path)
    second_mtime = second_cog.stat().st_mtime

    assert first_cog == second_cog
    assert second_mtime == first_mtime


def test_ensure_reference_imagery_cog_applies_valid_mask_as_alpha(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source.tif"
    valid_mask_path = tmp_path / "source_valid_mask.tif"
    cog_path = tmp_path / "cog" / "reference_imagery_cog.tif"
    _write_rgb_raster(source_raster_path)
    _write_valid_mask_raster(valid_mask_path)

    ensure_reference_imagery_cog(source_raster_path, cog_path, valid_mask_path=valid_mask_path)

    with rasterio.open(cog_path) as src:
        mask = src.dataset_mask()
        assert mask.shape == (512, 512)
        assert int(mask[0, 0]) == 0
        assert int(mask[256, 256]) == 255


def test_ensure_reference_imagery_cog_rejects_ungeoreferenced_source(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source_without_georef.tif"
    cog_path = tmp_path / "cog" / "reference_imagery_cog.tif"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        _write_ungeoreferenced_rgb_raster(source_raster_path)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            ensure_reference_imagery_cog(source_raster_path, cog_path)
    except ValueError as exc:
        assert "no CRS" in str(exc) or "identity transform" in str(exc)
    else:
        raise AssertionError("Ungeoreferenced source raster should not be promoted to COG reference imagery")


def test_render_reference_tile_png_returns_transparent_pixels_from_dataset_mask(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source.tif"
    valid_mask_path = tmp_path / "source_valid_mask.tif"
    cog_path = tmp_path / "cog" / "reference_imagery_cog.tif"
    _write_rgb_raster(source_raster_path)
    _write_valid_mask_raster(valid_mask_path)
    ensure_reference_imagery_cog(source_raster_path, cog_path, valid_mask_path=valid_mask_path)

    tile_bytes = render_reference_tile_png(cog_path, 8, 128, 128, tile_size=256)
    image = Image.open(BytesIO(tile_bytes)).convert("RGBA")
    alpha = np.array(image)[:, :, 3]
    assert int(alpha.min()) == 0
    assert int(alpha.max()) == 255


def test_get_temporal_project_does_not_hydrate_reference_cogs(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-read-only"
    _write_temporal_project_with_reference(settings, project_id)

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("project load must not hydrate reference imagery COGs")

    monkeypatch.setattr(reference_imagery_service, "ensure_reference_imagery_cog", fail_if_called)
    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{project_id}")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert response.json()["project_id"] == project_id
    assert response.json()["has_reference_layers"] is False
    assert response.json()["reference_layer_count"] == 0


def test_temporal_project_model_rejects_derived_reference_layer_fields() -> None:
    with pytest.raises(ValidationError):
        TemporalProject.model_validate(
            {
                "project_id": "temporal-strict",
                "name": "Strict",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "has_reference_layers": False,
                "reference_layer_count": 0,
            }
        )


def test_temporal_project_loader_strips_persisted_derived_reference_layer_fields(caplog, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-broken-derived-fields"
    _write_temporal_project_with_reference(settings, project_id)
    project_json_path = settings.temporal_projects_dir / project_id / "project.json"
    payload = json.loads(project_json_path.read_text())
    payload["has_reference_layers"] = False
    payload["reference_layer_count"] = 0
    project_json_path.write_text(json.dumps(payload, indent=2))

    with caplog.at_level("INFO"):
        project = get_temporal_project(project_id, settings)

    assert project.project_id == project_id
    assert not hasattr(project, "has_reference_layers")
    assert "TEMPORAL_PROJECT_STRIPPED_DERIVED_FIELDS" in caplog.text

    save_temporal_project(project, settings)
    saved_payload = json.loads(project_json_path.read_text())
    assert "has_reference_layers" not in saved_payload
    assert "reference_layer_count" not in saved_payload


def test_get_temporal_project_reports_reference_layer_metadata_when_layers_exist(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-read-only-with-reference-layers"
    _write_temporal_project_with_reference(settings, project_id)
    project_dir = settings.temporal_projects_dir / project_id
    _write_reference_layers_metadata(project_dir, count=2)

    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{project_id}")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert response.json()["has_reference_layers"] is True
    assert response.json()["reference_layer_count"] == 2


def test_reference_layers_listing_does_not_hydrate_reference_cogs(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-reference-layers-read-only"
    _write_temporal_project_with_reference(settings, project_id)

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("reference layer listing must not hydrate reference imagery COGs")

    monkeypatch.setattr(reference_imagery_service, "ensure_reference_imagery_cog", fail_if_called)
    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{project_id}/reference-layers")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["layer_id"] == "temporal-reference-WB_2024_R02"
    assert payload[0]["storage_strategy"] == "raster_tiles"


def test_reference_layers_repair_reuses_matching_project_cog(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    selected_release = "WB_2024_R02"
    source_project_id = "temporal-source-reference"
    target_project_id = "temporal-target-reference"
    shared_aoi = _test_aoi()

    source_cog = _write_temporal_project_with_reference(settings, source_project_id, selected_release=selected_release)
    source_payload_path = settings.temporal_projects_dir / source_project_id / "project.json"
    source_payload = json.loads(source_payload_path.read_text())
    source_payload["aoi_geojson"] = shared_aoi
    source_payload_path.write_text(json.dumps(source_payload, indent=2))
    _write_temporal_project_without_reference(
        settings,
        target_project_id,
        selected_release=selected_release,
        aoi_geojson=shared_aoi,
    )

    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{target_project_id}/reference-layers")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["layer_id"] == f"temporal-reference-{selected_release}"
    assert payload[0]["tilejson_url"]
    assert payload[0]["tiles_url_template"]
    target_cog = settings.temporal_projects_dir / target_project_id / "milestones" / selected_release / "reference_imagery_cog.tif"
    assert target_cog.is_file()
    assert target_cog.stat().st_size == source_cog.stat().st_size

    repaired_project = get_temporal_project(target_project_id, settings)
    assert repaired_project.milestones[0].reference_imagery is not None
    assert repaired_project.milestones[0].reference_imagery.cog_path == str(target_cog)


def test_reference_layers_repair_does_not_reuse_different_aoi(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    selected_release = "WB_2024_R02"
    source_project_id = "temporal-source-different-aoi"
    target_project_id = "temporal-target-different-aoi"

    _write_temporal_project_with_reference(settings, source_project_id, selected_release=selected_release)
    source_payload_path = settings.temporal_projects_dir / source_project_id / "project.json"
    source_payload = json.loads(source_payload_path.read_text())
    source_payload["aoi_geojson"] = _test_aoi()
    source_payload_path.write_text(json.dumps(source_payload, indent=2))
    _write_temporal_project_without_reference(
        settings,
        target_project_id,
        selected_release=selected_release,
        aoi_geojson=_test_aoi(offset=0.1),
    )

    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{target_project_id}/reference-layers")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert response.json() == []
    target_cog = settings.temporal_projects_dir / target_project_id / "milestones" / selected_release / "reference_imagery_cog.tif"
    assert not target_cog.exists()


def test_reference_layers_repair_ignores_additions_without_reference_source(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-additions-only"
    _write_temporal_project_without_reference(settings, project_id, aoi_geojson=_test_aoi())

    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{project_id}/reference-layers")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert response.json() == []


def test_reference_layers_repair_generates_from_matching_shared_mosaic(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    selected_release = "WB_2024_R02"
    project_id = "temporal-shared-mosaic-reference"
    aoi_geojson = _test_aoi()
    _write_temporal_project_without_reference(settings, project_id, selected_release=selected_release, aoi_geojson=aoi_geojson)

    mosaic_dir = settings.wayback_mosaic_cache_dir / "matching-shared-mosaic"
    mosaic_dir.mkdir(parents=True, exist_ok=True)
    _write_rgb_raster(mosaic_dir / "mosaic.tif")
    _write_valid_mask_raster(mosaic_dir / "valid_mask.tif")
    tile_range = tile_range_for_bbox(bounds_dict(parse_aoi_geometry(aoi_geojson)), settings.zoom)
    (mosaic_dir / "metadata.json").write_text(
        json.dumps(
            {
                "release_identifier": selected_release,
                "zoom": settings.zoom,
                "tile_range": list(tile_range),
            },
            indent=2,
        )
    )

    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{project_id}/reference-layers")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["layer_id"] == f"temporal-reference-{selected_release}"
    target_cog = settings.temporal_projects_dir / project_id / "milestones" / selected_release / "reference_imagery_cog.tif"
    assert target_cog.is_file()


def test_reference_layers_repair_matches_shared_mosaic_at_metadata_zoom(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", zoom=18)
    selected_release = "WB_2014_R05"
    project_id = "temporal-baseline-shared-mosaic-reference"
    aoi_geojson = _test_aoi()
    _write_temporal_project_without_reference(settings, project_id, selected_release=selected_release, aoi_geojson=aoi_geojson)

    mosaic_dir = settings.wayback_mosaic_cache_dir / "matching-shared-mosaic-z17"
    mosaic_dir.mkdir(parents=True, exist_ok=True)
    _write_rgb_raster(mosaic_dir / "mosaic.tif")
    _write_valid_mask_raster(mosaic_dir / "valid_mask.tif")
    metadata_zoom = 17
    tile_range = tile_range_for_bbox(bounds_dict(parse_aoi_geometry(aoi_geojson)), metadata_zoom)
    (mosaic_dir / "metadata.json").write_text(
        json.dumps(
            {
                "release_identifier": selected_release,
                "zoom": metadata_zoom,
                "tile_range": list(tile_range),
            },
            indent=2,
        )
    )

    client = _client_with_settings(settings)
    try:
        response = client.get(f"/api/temporal-projects/{project_id}/reference-layers")
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    target_cog = settings.temporal_projects_dir / project_id / "milestones" / selected_release / "reference_imagery_cog.tif"
    assert target_cog.is_file()


def test_temporal_project_load_generates_missing_buffer_layers_from_additions(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-buffer-discovery"
    project_dir = settings.temporal_projects_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    additions_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"change_id": 1},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-7.0000, 33.0000],
                            [-6.9995, 33.0000],
                            [-6.9995, 33.0005],
                            [-7.0000, 33.0005],
                            [-7.0000, 33.0000],
                        ]
                    ],
                },
            }
        ],
    }
    project = TemporalProject(
        project_id=project_id,
        name="Temporal buffer discovery",
        project_dir=str(project_dir),
        aoi_geojson=_test_aoi(),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        milestones=[
            {
                "release_identifier": "WB_2014_R05",
                "release_date": "2014-04-30",
                "additions_geojson": {"type": "FeatureCollection", "features": []},
            },
            {
                "release_identifier": "WB_2022_R03",
                "release_date": "2022-03-23",
                "additions_geojson": additions_geojson,
            },
        ],
    )
    (project_dir / "project.json").write_text(json.dumps(project.model_dump(mode="json"), indent=2))

    loaded = get_temporal_project(project_id, settings)

    assert loaded.milestones[0].buffer_layers_geojson == {}
    non_baseline_buffers = loaded.milestones[1].buffer_layers_geojson
    assert set(non_baseline_buffers) == {"10m", "15m", "20m"}
    assert all(len(payload["features"]) == 1 for payload in non_baseline_buffers.values())
    for name in ("building_change_buffer_10m.geojson", "building_change_buffer_15m.geojson", "building_change_buffer_20m.geojson"):
        assert (project_dir / "milestones" / "WB_2022_R03" / name).is_file()


def test_tilejson_metadata_lookup_touches_only_selected_release(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-selected-tilejson"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    requested_releases: list[str] = []
    original_resolver = reference_imagery_service.resolve_temporal_reference_cog_cached

    def spy_resolver(*, project_id, release_identifier, reference_imagery):  # noqa: ANN001
        requested_releases.append(release_identifier)
        return original_resolver(
            project_id=project_id,
            release_identifier=release_identifier,
            reference_imagery=reference_imagery,
        )

    monkeypatch.setattr("src.api.routes.temporal_projects.resolve_temporal_reference_cog_cached", spy_resolver)
    client = _client_with_settings(settings)
    try:
        response = client.get(
            f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json"
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert requested_releases == [selected_release]


def test_tilejson_uses_direct_selected_cog_without_full_project_hydration(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-direct-tilejson"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("tilejson endpoint must not hydrate the full temporal project when selected COG exists")

    monkeypatch.setattr("src.api.routes.temporal_projects.get_temporal_project_api", fail_if_called)
    client = _client_with_settings(settings)
    try:
        response = client.get(
            f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json"
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200


def test_tile_endpoint_uses_direct_selected_cog_without_full_project_hydration(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-direct-tile"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("tile endpoint must not hydrate the full temporal project when selected COG exists")

    monkeypatch.setattr("src.api.routes.temporal_projects.get_temporal_project_api", fail_if_called)
    client = _client_with_settings(settings)
    try:
        response = client.get(
            f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tiles/0/0/0.png"
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200


def test_tilejson_repairs_selected_release_metadata_when_saved_project_has_legacy_preview_only(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-selected-legacy-reference"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(
        settings,
        project_id,
        selected_release=selected_release,
        include_tile_metadata=False,
    )

    client = _client_with_settings(settings)
    try:
        response = client.get(
            f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json"
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["tiles"]) == 1
    assert payload["tiles"][0].startswith(
        f"http://testserver/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tiles/{{z}}/{{x}}/{{y}}.png?v="
    )
    project_payload = json.loads((settings.temporal_projects_dir / project_id / "project.json").read_text())
    assert project_payload["milestones"][1]["reference_imagery"]["storage_strategy"] is None
    assert project_payload["milestones"][1]["reference_imagery"]["tilejson_url"] is None


def test_tilejson_rejects_ungeoreferenced_reference_cog(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-invalid-cog"
    selected_release = "WB_2024_R02"
    cog_path = _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        _write_ungeoreferenced_rgb_raster(cog_path)
    reference_imagery_service._REFERENCE_IMAGERY_METADATA_CACHE.clear()

    client = _client_with_settings(settings)
    try:
        response = client.get(
            f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json"
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 404


def test_tilejson_metadata_cache_reuses_file_metadata(monkeypatch, tmp_path: Path) -> None:
    cog_path = tmp_path / "reference_imagery_cog.tif"
    _write_rgb_raster(cog_path)
    reference_imagery_service._REFERENCE_IMAGERY_METADATA_CACHE.clear()
    calls = {"inspect": 0}
    original_inspect = reference_imagery_service._inspect_reference_cog

    def spy_inspect(path: Path):  # noqa: ANN001
        calls["inspect"] += 1
        assert path == cog_path.resolve()
        return original_inspect(path)

    monkeypatch.setattr(reference_imagery_service, "_inspect_reference_cog", spy_inspect)
    imagery = TemporalReferenceImagery(storage_strategy="raster_tiles", cog_path=str(cog_path), tile_size=256)

    first = reference_imagery_service.resolve_temporal_reference_cog_cached(
        project_id="temporal-cache",
        release_identifier="WB_2024_R02",
        reference_imagery=imagery,
    )
    second = reference_imagery_service.resolve_temporal_reference_cog_cached(
        project_id="temporal-cache",
        release_identifier="WB_2024_R02",
        reference_imagery=imagery,
    )

    assert first == second
    assert calls == {"inspect": 1}


def test_tilejson_payload_cache_hits_on_second_request(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source.tif"
    _write_rgb_raster(source_raster_path)
    imagery = build_temporal_reference_imagery(
        project_id="temporal-demo",
        project_dir=tmp_path / "project",
        release_identifier="WB_2024_R01",
        source=TemporalReferenceSource(
            image_path=None,
            image_png_data_url=None,
            raster_bounds_wgs84=None,
            source_raster_path=str(source_raster_path),
        ),
    )
    cog_info = resolve_temporal_reference_cog(imagery)
    assert cog_info is not None

    clear_reference_tilejson_cache()
    first_payload, first_hit = build_reference_tilejson_payload_cached(
        project_id="temporal-demo",
        release_identifier="WB_2024_R01",
        cog_info=cog_info,
        name="temporal-demo:WB_2024_R01",
        tiles_url="http://example.test/tiles/{z}/{x}/{y}.png?v=123",
    )
    second_payload, second_hit = build_reference_tilejson_payload_cached(
        project_id="temporal-demo",
        release_identifier="WB_2024_R01",
        cog_info=cog_info,
        name="temporal-demo:WB_2024_R01",
        tiles_url="http://example.test/tiles/{z}/{x}/{y}.png?v=123",
    )

    assert first_hit is False
    assert second_hit is True
    assert first_payload == second_payload


def test_tilejson_payload_cache_key_changes_when_cog_mtime_changes(tmp_path: Path) -> None:
    source_raster_path = tmp_path / "source.tif"
    _write_rgb_raster(source_raster_path)
    imagery = build_temporal_reference_imagery(
        project_id="temporal-demo",
        project_dir=tmp_path / "project",
        release_identifier="WB_2024_R01",
        source=TemporalReferenceSource(
            image_path=None,
            image_png_data_url=None,
            raster_bounds_wgs84=None,
            source_raster_path=str(source_raster_path),
        ),
    )
    cog_info = resolve_temporal_reference_cog(imagery)
    assert cog_info is not None

    clear_reference_tilejson_cache()
    _, first_hit = build_reference_tilejson_payload_cached(
        project_id="temporal-demo",
        release_identifier="WB_2024_R01",
        cog_info=cog_info,
        name="temporal-demo:WB_2024_R01",
        tiles_url="http://example.test/tiles/{z}/{x}/{y}.png?v=123",
    )
    stat = cog_info.cog_path.stat()
    os.utime(cog_info.cog_path, ns=(stat.st_atime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))
    _, second_hit = build_reference_tilejson_payload_cached(
        project_id="temporal-demo",
        release_identifier="WB_2024_R01",
        cog_info=cog_info,
        name="temporal-demo:WB_2024_R01",
        tiles_url="http://example.test/tiles/{z}/{x}/{y}.png?v=123",
    )

    assert first_hit is False
    assert second_hit is False


def test_repeated_tilejson_and_tile_requests_do_not_modify_cog_mtime(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-cog-read-only"
    selected_release = "WB_2024_R02"
    cog_path = _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    original_mtime = cog_path.stat().st_mtime

    client = _client_with_settings(settings)
    try:
        tilejson_url = f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json"
        tile_url = f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tiles/0/0/0.png"
        assert client.get(tilejson_url).status_code == 200
        assert client.get(tilejson_url).status_code == 200
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", NotGeoreferencedWarning)
            first_tile = client.get(tile_url)
            second_tile = client.get(tile_url)
        assert first_tile.status_code == 200
        assert second_tile.status_code == 200
        assert first_tile.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert not [item for item in caught if issubclass(item.category, NotGeoreferencedWarning)]
    finally:
        _clear_dependency_overrides()


def test_reference_tile_persistent_cache_miss_then_hit(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-persistent-tile-cache"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    tile_url = f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tiles/0/0/0.png"

    client = _client_with_settings(settings)
    try:
        first = client.get(tile_url)
        assert first.status_code == 200
        cached_files = list((settings.reference_tile_cache_dir / project_id / selected_release).rglob("0.png"))
        assert cached_files

        def fail_render(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("COG rendering must not run for a persistent cache hit")

        monkeypatch.setattr(reference_imagery_service, "render_reference_tile_png_cached", fail_render)
        second = client.get(tile_url)
        assert second.status_code == 200
        assert second.content == first.content
    finally:
        _clear_dependency_overrides()


def test_reference_tile_cache_path_includes_version_and_coordinates(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-cache-path"
    selected_release = "WB_2024_R02"
    cog_path = _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    imagery = TemporalReferenceImagery(cog_path=str(cog_path), tile_size=256)
    cog_info = resolve_temporal_reference_cog_cached(
        project_id=project_id,
        release_identifier=selected_release,
        reference_imagery=imagery,
    )
    assert cog_info is not None
    first_version = reference_imagery_version_token(cog_info)
    first_path = reference_tile_cache_path(
        settings.reference_tile_cache_dir,
        project_id=project_id,
        release_identifier=selected_release,
        cog_version=first_version,
        z=18,
        x=125498,
        y=105102,
    )
    assert first_path == settings.reference_tile_cache_dir / project_id / selected_release / first_version / "18" / "125498" / "105102.png"

    os.utime(cog_path, (cog_path.stat().st_atime, cog_path.stat().st_mtime + 10))
    updated_info = resolve_temporal_reference_cog_cached(
        project_id=project_id,
        release_identifier=selected_release,
        reference_imagery=imagery,
    )
    assert updated_info is not None
    second_version = reference_imagery_version_token(updated_info)
    assert second_version != first_version
    second_path = reference_tile_cache_path(
        settings.reference_tile_cache_dir,
        project_id=project_id,
        release_identifier=selected_release,
        cog_version=second_version,
        z=18,
        x=125498,
        y=105102,
    )
    assert second_path != first_path


def test_reference_tile_prewarm_generates_then_hits(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-prewarm-cache"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    prewarm_url = f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/prewarm"
    payload = {"tiles": [{"z": 0, "x": 0, "y": 0}, {"z": 1, "x": 0, "y": 0}]}

    client = _client_with_settings(settings)
    try:
        first = client.post(prewarm_url, json=payload)
        second = client.post(prewarm_url, json=payload)
    finally:
        _clear_dependency_overrides()

    assert first.status_code == 200
    assert first.json()["requested"] == 2
    assert first.json()["hits"] == 0
    assert first.json()["misses"] == 2
    assert first.json()["generated"] == 2
    assert first.json()["failed"] == 0
    assert second.status_code == 200
    assert second.json()["hits"] == 2
    assert second.json()["misses"] == 0


def test_reference_tile_prewarm_rejects_limit_and_invalid_coordinates(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", reference_tile_prewarm_max_tiles=1)
    project_id = "temporal-prewarm-validation"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    prewarm_url = f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/prewarm"

    client = _client_with_settings(settings)
    try:
        too_many = client.post(
            prewarm_url,
            json={"tiles": [{"z": 0, "x": 0, "y": 0}, {"z": 0, "x": 0, "y": 0}]},
        )
        invalid = client.post(prewarm_url, json={"tiles": [{"z": 1, "x": 2, "y": 0}]})
    finally:
        _clear_dependency_overrides()

    assert too_many.status_code == 400
    assert invalid.status_code == 400


def test_reference_tile_cache_write_failure_falls_back_to_rendered_response(monkeypatch, caplog, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-cache-write-fallback"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)
    tile_url = f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tiles/0/0/0.png"

    def fail_write(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("read-only cache")

    monkeypatch.setattr(reference_imagery_service, "_write_tile_cache_atomic", fail_write)
    client = _client_with_settings(settings)
    try:
        with caplog.at_level("WARNING"):
            response = client.get(tile_url)
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "TILE_CACHE_WRITE_FAILED" in caplog.text


def test_client_log_relay_accepts_temporal_reference_events(caplog, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", enable_client_log_relay=True)
    client = _client_with_settings(settings)
    try:
        with caplog.at_level("INFO"):
            response = client.post(
                "/api/dev/client-log",
                json={
                    "event": "TEMPORAL_REFERENCE_SOURCE_REUSE",
                    "payload": {"projectId": "demo", "releaseIdentifier": "WB_2024_R02"},
                    "timestamp": "2026-05-08T10:00:00Z",
                    "source": "frontend",
                },
            )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 204
    assert "CLIENT_LOG event=TEMPORAL_REFERENCE_SOURCE_REUSE" in caplog.text


def test_client_log_relay_is_disabled_by_default(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", enable_client_log_relay=False)
    client = _client_with_settings(settings)
    try:
        response = client.post(
            "/api/dev/client-log",
            json={
                "event": "TEMPORAL_REFERENCE_SOURCE_REUSE",
                "payload": {"projectId": "demo"},
                "timestamp": "2026-05-08T10:00:00Z",
                "source": "frontend",
            },
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 404


def test_client_log_relay_ignores_unrelated_events(caplog, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", enable_client_log_relay=True)
    client = _client_with_settings(settings)
    try:
        with caplog.at_level("INFO"):
            response = client.post(
                "/api/dev/client-log",
                json={
                    "event": "UNRELATED_EVENT",
                    "payload": {"projectId": "demo"},
                    "timestamp": "2026-05-08T10:00:00Z",
                    "source": "frontend",
                },
            )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 204
    assert "CLIENT_LOG event=UNRELATED_EVENT" not in caplog.text


def test_client_log_relay_malformed_payload_does_not_crash_backend(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", enable_client_log_relay=True)
    client = _client_with_settings(settings)
    try:
        response = client.post(
            "/api/dev/client-log",
            json={
                "event": "TEMPORAL_REFERENCE_SOURCE_REUSE",
                "payload": "not-an-object",
                "timestamp": "2026-05-08T10:00:00Z",
                "source": "frontend",
            },
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code in {204, 422}


def test_tilejson_includes_version_token_and_is_revalidatable(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    project_id = "temporal-versioned-tiles"
    selected_release = "WB_2024_R02"
    _write_temporal_project_with_reference(settings, project_id, selected_release=selected_release)

    client = _client_with_settings(settings)
    try:
        response = client.get(
            f"/api/temporal-projects/{project_id}/milestones/{selected_release}/reference/tilejson.json"
        )
    finally:
        _clear_dependency_overrides()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=0, must-revalidate"
    payload = response.json()
    assert len(payload["tiles"]) == 1
    assert "?v=" in payload["tiles"][0]
