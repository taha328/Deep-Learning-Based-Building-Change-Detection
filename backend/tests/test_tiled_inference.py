from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.domain.tiled_inference import (
    InferenceTile,
    TiledInferenceConfig,
    iter_inference_tiles,
    make_bandon_patch_predictor,
    make_difference_patch_predictor,
    make_synthetic_square_patch_predictor,
    run_tiled_inference,
    select_inference_mode,
)
from src.config import Settings


def _settings(tmp_path: Path, **overrides):
    values = {
        "runtime_cache_dir": tmp_path / "runtime",
        "inference_tiled_mode_auto": True,
        "inference_tile_size": 1024,
        "inference_tile_overlap": 128,
        "inference_tile_batch_size": 1,
        "inference_max_in_memory_pixels": 25_000_000,
        "inference_heavy_batch_tile_threshold": 2000,
    }
    values.update(overrides)
    values["runtime_cache_dir"].mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(**values)


def _write_rgb(path: Path, data: np.ndarray) -> None:
    profile = {
        "driver": "GTiff",
        "width": data.shape[1],
        "height": data.shape[0],
        "count": 3,
        "dtype": "uint8",
        "crs": "EPSG:3857",
        "transform": from_origin(0, 1000, 1, 1),
        "tiled": True,
        "blockxsize": 16,
        "blockysize": 16,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.moveaxis(data, -1, 0))


def _write_mask(path: Path, shape: tuple[int, int]) -> None:
    profile = {
        "driver": "GTiff",
        "width": shape[1],
        "height": shape[0],
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:3857",
        "transform": from_origin(0, 1000, 1, 1),
        "tiled": True,
        "blockxsize": 16,
        "blockysize": 16,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.ones(shape, dtype=np.uint8), 1)


def test_bandon_patch_predictor_uses_configured_threshold_not_runner_argmax(monkeypatch, tmp_path: Path) -> None:
    probability = np.array([[0.35, 0.49], [0.50, 0.90]], dtype=np.float32)
    monkeypatch.setattr(
        "src.domain.bandon_runner.run_bandon_inference",
        lambda **kwargs: SimpleNamespace(
            change_probability=probability,
            change_mask=np.array([[False, False], [True, True]], dtype=bool),
            metadata={},
        ),
    )
    predictor = make_bandon_patch_predictor(
        settings=Settings(runtime_cache_dir=tmp_path / "runtime"),
        effective_backend="bandon_mps",
        threshold=0.35,
    )

    prediction = predictor(
        tile=InferenceTile(index=0, window=rasterio.windows.Window(0, 0, 2, 2)),
        t1_rgb=np.zeros((2, 2, 3), dtype=np.uint8),
        t2_rgb=np.zeros((2, 2, 3), dtype=np.uint8),
        t1_valid_mask=None,
        t2_valid_mask=None,
        aoi_mask=None,
        work_dir=tmp_path / "work",
    )

    assert np.array_equal(prediction.mask, np.ones((2, 2), dtype=bool))


def test_inference_mode_switches_to_tiled_for_large_pixel_count(tmp_path: Path) -> None:
    settings = _settings(tmp_path, inference_max_in_memory_pixels=10_000)
    decision = select_inference_mode(width=200, height=200, tile_count=1, settings=settings)
    assert decision.mode == "tiled"
    assert decision.reason == "memory_guard"


def test_inference_mode_keeps_imagery_tile_cache_backend_separate_from_inference(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        inference_max_in_memory_pixels=10_000_000,
        inference_heavy_batch_tile_threshold=2000,
    )
    decision = select_inference_mode(width=512, height=512, tile_count=2500, settings=settings)
    assert decision.mode == "tiled"
    assert decision.reason == "heavy_tile_batch"


def test_tile_grid_uses_configured_overlap_stride() -> None:
    tiles = iter_inference_tiles(2048, 1024, tile_size=1024, overlap=128)
    assert [int(tile.window.col_off) for tile in tiles] == [0, 768, 1024]
    assert [int(tile.window.row_off) for tile in tiles] == [0, 0, 0]
    assert int(tiles[-1].window.width) == 1024


def test_tiled_inference_writes_raster_state_and_geojsonl(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    height, width = 64, 64
    t1 = np.zeros((height, width, 3), dtype=np.uint8)
    t2 = t1.copy()
    t2[20:40, 20:40, :] = 255
    t1_path = tmp_path / "t1.tif"
    t2_path = tmp_path / "t2.tif"
    t1_mask_path = tmp_path / "t1_valid.tif"
    t2_mask_path = tmp_path / "t2_valid.tif"
    _write_rgb(t1_path, t1)
    _write_rgb(t2_path, t2)
    _write_mask(t1_mask_path, (height, width))
    _write_mask(t2_mask_path, (height, width))

    result = run_tiled_inference(
        t1_mosaic_path=t1_path,
        t2_mosaic_path=t2_path,
        t1_valid_mask_path=t1_mask_path,
        t2_valid_mask_path=t2_mask_path,
        output_dir=tmp_path / "out",
        run_id="unit-run",
        settings=settings,
        config=TiledInferenceConfig(
            tile_size=32,
            overlap=4,
            batch_size=1,
            threshold=0.2,
            max_in_memory_pixels=25_000_000,
            heavy_batch_tile_threshold=2000,
        ),
        predictor=make_difference_patch_predictor(threshold=0.2),
        release_t1="WB_2020_R04",
        release_t2="WB_2026_R04",
    )

    assert result.mask_path.exists()
    assert result.probability_path.exists()
    assert result.geojsonl_path.exists()
    assert result.state_path.exists()
    assert result.metadata_path.exists()
    with rasterio.open(result.mask_path) as src:
        assert (src.width, src.height) == (width, height)
        assert int(src.read(1).sum()) > 0
    metadata = json.loads(result.metadata_path.read_text())
    assert metadata["mode"] == "tiled"
    assert metadata["tile_size"] == 32
    assert metadata["feature_count"] > 0
    state = json.loads(result.state_path.read_text())
    assert state["processed_tiles"] == result.processed_tiles
    assert state["completed_chunk_count"] == result.processed_tiles
    assert len(state["completed_chunks"]) == result.processed_tiles


def test_tiled_inference_resumes_completed_chunks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    height, width = 64, 64
    t1 = np.zeros((height, width, 3), dtype=np.uint8)
    t2 = t1.copy()
    t2[20:40, 20:40, :] = 255
    t1_path = tmp_path / "t1.tif"
    t2_path = tmp_path / "t2.tif"
    t1_mask_path = tmp_path / "t1_valid.tif"
    t2_mask_path = tmp_path / "t2_valid.tif"
    _write_rgb(t1_path, t1)
    _write_rgb(t2_path, t2)
    _write_mask(t1_mask_path, (height, width))
    _write_mask(t2_mask_path, (height, width))
    kwargs = {
        "t1_mosaic_path": t1_path,
        "t2_mosaic_path": t2_path,
        "t1_valid_mask_path": t1_mask_path,
        "t2_valid_mask_path": t2_mask_path,
        "output_dir": tmp_path / "out",
        "run_id": "resume-run",
        "settings": settings,
        "config": TiledInferenceConfig(
            tile_size=32,
            overlap=4,
            batch_size=1,
            threshold=0.2,
            max_in_memory_pixels=25_000_000,
            heavy_batch_tile_threshold=2000,
        ),
        "predictor": make_difference_patch_predictor(threshold=0.2),
        "release_t1": "WB_2020_R04",
        "release_t2": "WB_2026_R04",
    }
    first = run_tiled_inference(**kwargs, max_tiles=2)
    second = run_tiled_inference(**kwargs, max_tiles=2)
    assert first.processed_tiles == 2
    assert second.processed_tiles == 2
    assert second.skipped_tiles == 2
    state = json.loads(second.state_path.read_text())
    assert state["completed_chunk_count"] == 2
    assert state["skipped_tiles_this_run"] == 2


def test_synthetic_predictor_streams_vector_features(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    height, width = 64, 64
    t1 = np.zeros((height, width, 3), dtype=np.uint8)
    t2 = t1.copy()
    t1_path = tmp_path / "t1.tif"
    t2_path = tmp_path / "t2.tif"
    _write_rgb(t1_path, t1)
    _write_rgb(t2_path, t2)
    result = run_tiled_inference(
        t1_mosaic_path=t1_path,
        t2_mosaic_path=t2_path,
        t1_valid_mask_path=None,
        t2_valid_mask_path=None,
        output_dir=tmp_path / "out",
        run_id="synthetic-run",
        settings=settings,
        config=TiledInferenceConfig(
            tile_size=32,
            overlap=4,
            batch_size=1,
            threshold=0.2,
            max_in_memory_pixels=25_000_000,
            heavy_batch_tile_threshold=2000,
        ),
        predictor=make_synthetic_square_patch_predictor(every_n_tiles=1),
        release_t1="WB_2020_R04",
        release_t2="WB_2026_R04",
        max_tiles=1,
    )
    lines = [line for line in result.geojsonl_path.read_text().splitlines() if line]
    assert result.feature_count == 1
    assert len(lines) == 1
    feature = json.loads(lines[0])
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["area_m2"] > 0
