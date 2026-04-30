from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.config import Settings
from src.domain.coregistration import CoregistrationResult, coregister_t1_to_t2_reprojection_only


def _write_single_band(path: Path, array: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=1,
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=from_origin(0.0, float(array.shape[0]), 1.0, 1.0),
    ) as dst:
        dst.write(array, 1)


def _write_rgb(path: Path, array: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=array.shape[2],
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=from_origin(0.0, float(array.shape[0]), 1.0, 1.0),
    ) as dst:
        for band_index in range(array.shape[2]):
            dst.write(array[:, :, band_index], band_index + 1)


def test_coregistration_uses_reprojection_only_paths(tmp_path) -> None:
    settings = Settings()
    t2_path = tmp_path / "t2.tif"
    t1_path = tmp_path / "t1.tif"
    t2_mask_path = tmp_path / "t2_valid.tif"
    t1_mask_path = tmp_path / "t1_valid.tif"
    rgb = np.zeros((4, 4, 3), dtype=np.uint8)
    valid = np.ones((4, 4), dtype=np.uint8)
    _write_rgb(t2_path, rgb)
    _write_rgb(t1_path, rgb)
    _write_single_band(t2_mask_path, valid)
    _write_single_band(t1_mask_path, valid)

    result = coregister_t1_to_t2_reprojection_only(
        reference_image_path=t2_path,
        target_image_path=t1_path,
        reference_valid_mask_path=t2_mask_path,
        target_valid_mask_path=t1_mask_path,
        output_dir=tmp_path,
        settings=settings,
    )

    assert isinstance(result, CoregistrationResult)
    assert result.corrected_t1_path == t1_path
    assert result.corrected_t1_valid_mask_path == t1_mask_path
    assert result.diagnostics.method == "reprojection_only"
    assert result.diagnostics.corrected_t1_path == str(t1_path)
    assert result.diagnostics.corrected_t1_valid_mask_path == str(t1_mask_path)
    assert not (tmp_path / "t1_invalid_mask_for_arosics.tif").exists()
    assert not (tmp_path / "t2_invalid_mask_for_arosics.tif").exists()


def test_coregistration_does_not_import_arosics(tmp_path, monkeypatch) -> None:
    settings = Settings()
    t2_path = tmp_path / "t2.tif"
    t1_path = tmp_path / "t1.tif"
    t2_mask_path = tmp_path / "t2_valid.tif"
    t1_mask_path = tmp_path / "t1_valid.tif"
    rgb = np.zeros((2, 2, 3), dtype=np.uint8)
    valid = np.ones((2, 2), dtype=np.uint8)
    _write_rgb(t2_path, rgb)
    _write_rgb(t1_path, rgb)
    _write_single_band(t2_mask_path, valid)
    _write_single_band(t1_mask_path, valid)

    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "arosics":
            raise AssertionError("reprojection-only alignment must not import arosics")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    result = coregister_t1_to_t2_reprojection_only(
        reference_image_path=t2_path,
        target_image_path=t1_path,
        reference_valid_mask_path=t2_mask_path,
        target_valid_mask_path=t1_mask_path,
        output_dir=tmp_path,
        settings=settings,
    )

    assert result.diagnostics.method == "reprojection_only"
