from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_bounds

from src.config import Settings
from src.domain.exports import export_bandon_outputs
from src.domain.wayback import WaybackRelease
from src.schemas import ValidationRequest
from src.services.validation import validate_request


def _release(identifier: str, release_date: date) -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=release_date,
        label=f"{release_date.isoformat()} | {identifier}",
        release_num=1,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://example.com/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.png",
    )


def _releases() -> list[WaybackRelease]:
    return [
        _release("WB_2026_R04", date(2026, 4, 30)),
        _release("WB_2026_R05", date(2026, 5, 28)),
    ]


def _aoi(delta: float) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [[
            [-7.0, 33.0],
            [-7.0 + delta, 33.0],
            [-7.0 + delta, 33.0 + delta],
            [-7.0, 33.0 + delta],
            [-7.0, 33.0],
        ]],
    }


def test_too_small_bandon_aoi_is_blocked_before_inference(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request = ValidationRequest(
        aoi_geojson=_aoi(0.0002),
        t1_release="WB_2026_R04",
        t2_release="WB_2026_R05",
        mode="fast_preview",
        inference_backend="bandon_mps",
    )

    response, prepared = validate_request(request, releases=_releases(), settings=settings)

    assert prepared is None
    assert response.valid is False
    assert response.details["estimated_model_input_width_px"] == 256
    assert response.details["estimated_model_input_height_px"] == 256
    assert any("BANDON requires at least 513x513 pixels" in message for message in response.blocking_errors)


def test_phase2c_smoke_aoi_remains_valid_for_bandon(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request = ValidationRequest(
        aoi_geojson=_aoi(0.0025),
        t1_release="WB_2026_R04",
        t2_release="WB_2026_R05",
        mode="fast_preview",
        inference_backend="bandon_mps",
    )

    response, prepared = validate_request(request, releases=_releases(), settings=settings)

    assert prepared is not None
    assert response.valid is True
    assert response.blocking_errors == []
    assert response.details["estimated_model_input_width_px"] == 768
    assert response.details["estimated_model_input_height_px"] == 768


def test_bandon_exports_do_not_expose_cleaned_tmp_metadata_path(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    request_dir = runtime_dir / "requests" / "phase2d-run"
    tmp_dir = runtime_dir / "tmp" / "phase2d-run" / "bandon_run"
    request_dir.mkdir(parents=True)
    tmp_dir.mkdir(parents=True)
    bandon_metadata_path = tmp_dir / "run_metadata.json"
    bandon_metadata_path.write_text("{}", encoding="utf-8")

    reference_raster_path = tmp_path / "reference.tif"
    data = np.zeros((8, 8), dtype=np.uint8)
    with rasterio.open(
        reference_raster_path,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(-8, 33, -7, 34, 8, 8),
    ) as dst:
        dst.write(data, 1)

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    probability = np.zeros((8, 8), dtype=np.float32)
    mask = np.zeros((8, 8), dtype=bool)
    labels = np.zeros((8, 8), dtype=np.uint16)
    empty_geojson = {"type": "FeatureCollection", "features": []}

    _previews, artifacts, _zip_path, _tables = export_bandon_outputs(
        result_dir=request_dir,
        reference_raster_path=reference_raster_path,
        t1_rgb=rgb,
        t2_rgb=rgb,
        change_prob=probability,
        change_mask=mask,
        change_labels=labels,
        change_polygons_df=pd.DataFrame({"change_id": []}),
        change_polygons_geojson=empty_geojson,
        change_blocks_df=pd.DataFrame({"block_id": []}),
        change_blocks_geojson=empty_geojson,
        buffer_layers={},
        summary_df=pd.DataFrame({"metric": []}),
        bandon_metadata_path=bandon_metadata_path,
    )

    assert all("runtime/tmp" not in artifact.path for artifact in artifacts)
    assert all(artifact.name != "bandon_run_metadata_json" for artifact in artifacts)
    assert any(artifact.path == str(request_dir / "change_probability.tif") for artifact in artifacts)
