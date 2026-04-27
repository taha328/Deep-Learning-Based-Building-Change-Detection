from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.config import Settings
from src.domain.coregistration import (
    CoregistrationResult,
    _invert_valid_mask_to_baddata_mask,
    coregister_t1_to_t2_with_arosics,
)


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


def test_invert_valid_mask_to_baddata_mask(tmp_path) -> None:
    valid_path = tmp_path / "valid_mask.tif"
    invalid_path = tmp_path / "invalid_mask.tif"
    valid_mask = np.array([[1, 0], [1, 1]], dtype=np.uint8)
    _write_single_band(valid_path, valid_mask)

    _invert_valid_mask_to_baddata_mask(valid_path, invalid_path)

    with rasterio.open(invalid_path) as src:
        invalid_mask = src.read(1)

    expected = np.array([[0, 1], [0, 0]], dtype=np.uint8)
    assert np.array_equal(invalid_mask, expected)


def test_coregistration_falls_back_when_arosics_is_missing(tmp_path, monkeypatch) -> None:
    settings = Settings(arosics_enabled=True, arosics_fallback_to_reprojection=True)
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

    monkeypatch.setattr(
        "src.domain.coregistration._get_arosics_classes",
        lambda: (_ for _ in ()).throw(ImportError("No module named 'arosics'")),
    )

    result = coregister_t1_to_t2_with_arosics(
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
    assert result.diagnostics.used_arosics is False
    assert result.diagnostics.method == "reprojection_fallback"
    assert "AROSICS is unavailable" in (result.diagnostics.fallback_reason or "")


def test_coregistration_success_uses_local_coreg_and_deshifts_valid_mask(tmp_path, monkeypatch) -> None:
    settings = Settings(arosics_enabled=True, arosics_fallback_to_reprojection=False)
    t2_path = tmp_path / "t2_scene.tif"
    t1_path = tmp_path / "t1_scene.tif"
    t2_mask_path = tmp_path / "t2_scene_valid.tif"
    t1_mask_path = tmp_path / "t1_scene_valid.tif"
    rgb = np.zeros((4, 4, 3), dtype=np.uint8)
    valid = np.ones((4, 4), dtype=np.uint8)
    _write_rgb(t2_path, rgb)
    _write_rgb(t1_path, rgb)
    _write_single_band(t2_mask_path, valid)
    _write_single_band(t1_mask_path, valid)

    call_log: dict[str, object] = {}

    class FakeCOREGLocal:
        def __init__(self, ref_path, tgt_path, **kwargs) -> None:
            call_log["ref_path"] = ref_path
            call_log["tgt_path"] = tgt_path
            call_log["kwargs"] = kwargs
            self.coreg_info = {"GCPList": [(1, 2), (3, 4), (5, 6)]}

        def correct_shifts(self) -> None:
            shutil.copyfile(t1_path, Path(call_log["kwargs"]["path_out"]))

    class FakeDESHIFTER:
        def __init__(self, src_path, coreg_info, **kwargs) -> None:
            call_log["mask_src_path"] = src_path
            call_log["mask_coreg_info"] = coreg_info
            call_log["mask_kwargs"] = kwargs

        def correct_shifts(self) -> None:
            shutil.copyfile(t1_mask_path, Path(call_log["mask_kwargs"]["path_out"]))

    monkeypatch.setattr(
        "src.domain.coregistration._get_arosics_classes",
        lambda: (FakeCOREGLocal, FakeDESHIFTER),
    )

    result = coregister_t1_to_t2_with_arosics(
        reference_image_path=t2_path,
        target_image_path=t1_path,
        reference_valid_mask_path=t2_mask_path,
        target_valid_mask_path=t1_mask_path,
        output_dir=tmp_path,
        settings=settings,
    )

    kwargs = call_log["kwargs"]
    assert call_log["ref_path"] != str(t2_path)
    assert call_log["tgt_path"] != str(t1_path)
    assert " " not in call_log["ref_path"]
    assert " " not in call_log["tgt_path"]
    assert kwargs["grid_res"] == settings.arosics_grid_res
    assert kwargs["window_size"] == (settings.arosics_window_size, settings.arosics_window_size)
    assert kwargs["mask_baddata_ref"].endswith("reference_invalid.tif")
    assert kwargs["mask_baddata_tgt"].endswith("target_invalid.tif")
    assert kwargs["align_grids"] is settings.arosics_align_grids
    assert kwargs["match_gsd"] is settings.arosics_match_gsd
    assert kwargs["resamp_alg_calc"] == settings.arosics_resamp_alg_calc
    assert kwargs["resamp_alg_deshift"] == settings.arosics_resamp_alg_deshift
    assert result.diagnostics.used_arosics is True
    assert result.diagnostics.method == "arosics_local"
    assert result.diagnostics.tie_point_count == 3
    assert Path(result.corrected_t1_path).exists()
    assert Path(result.corrected_t1_valid_mask_path).exists()
    assert call_log["mask_coreg_info"] == {"GCPList": [(1, 2), (3, 4), (5, 6)]}
