from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from src.config import Settings
from src.domain.cache import load_cached_response


def _write_test_raster(path: Path) -> None:
    width = 8
    height = 8
    bounds = (-8.0, 33.0, -7.0, 34.0)
    transform = from_bounds(*bounds, width=width, height=height)
    data = np.stack(
        [
            np.full((height, width), 40, dtype=np.uint8),
            np.full((height, width), 110, dtype=np.uint8),
            np.full((height, width), 180, dtype=np.uint8),
        ]
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=3,
        dtype=data.dtype,
        crs="EPSG:3857",
        transform=transform,
    ) as dst:
        dst.write(data)


def test_cached_response_is_upgraded_with_missing_raster_georeferencing(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    request_hash = "deadbeef00000000deadbeef"
    result_dir = settings.request_cache_dir / request_hash
    result_dir.mkdir(parents=True, exist_ok=True)

    _write_test_raster(result_dir / "t1_wayback_rgb.tif")
    assert not (result_dir / "t1_WB_2022_R03_z19.png").exists()

    legacy_payload = {
        "success": True,
        "summary": {
            "request_hash": request_hash,
            "mode": "full_run",
            "estimated_area_m2": 1.0,
            "tile_count_t1": 1,
            "tile_count_t2": 1,
            "total_new_buildings": 0,
            "total_building_blocks": 0,
            "total_new_building_area_m2": 0.0,
            "total_building_block_area_m2": 0.0,
        },
        "preview_images": {
            "t1_preview_path": str(result_dir / "t1_preview.png"),
            "t2_preview_path": str(result_dir / "t2_preview.png"),
            "change_probability_preview_path": str(result_dir / "change_probability_preview.png"),
            "change_overlay_preview_path": str(result_dir / "change_overlay_preview.png"),
            "raster_bounds_wgs84": None,
            "raster_bounds_native": None,
            "raster_crs": None,
            "raster_transform": None,
            "raster_size": None,
        },
        "buffer_layers_geojson": {},
        "artifacts": [],
    }
    response_path = result_dir / "run_response.json"
    response_path.write_text(json.dumps(legacy_payload, indent=2))

    response = load_cached_response(settings, request_hash)
    assert response is not None
    assert response.preview_images is not None
    assert response.preview_images.raster_bounds_wgs84 is not None
    assert response.preview_images.raster_bounds_native == [-8.0, 33.0, -7.0, 34.0]
    assert response.preview_images.raster_crs == "EPSG:3857"
    assert response.preview_images.raster_transform is not None
    assert response.preview_images.raster_size == [8, 8]

    reloaded_payload = json.loads(response_path.read_text())
    upgraded_preview = reloaded_payload.get("preview_images", {})
    assert upgraded_preview.get("raster_bounds_wgs84")
    assert upgraded_preview.get("raster_bounds_native") == [-8.0, 33.0, -7.0, 34.0]
    assert upgraded_preview.get("raster_crs") == "EPSG:3857"
