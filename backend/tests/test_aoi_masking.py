from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.services.processing import _apply_aoi_mask_to_aligned_inputs


def test_apply_aoi_mask_to_aligned_inputs_masks_outside_pixels(tmp_path) -> None:
    reference_path = tmp_path / "reference.tif"
    transform = from_origin(-1.0, 1.0, 0.5, 0.5)
    data = np.ones((1, 4, 4), dtype=np.uint8)
    with rasterio.open(
        reference_path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data)

    arr_t1 = np.full((4, 4, 3), 100, dtype=np.uint8)
    arr_t2 = np.full((4, 4, 3), 120, dtype=np.uint8)
    t1_valid_mask = np.ones((4, 4), dtype=bool)
    t2_valid_mask = np.ones((4, 4), dtype=bool)
    # Keep only top-left quarter in AOI.
    aoi = {
        "type": "Polygon",
        "coordinates": [[[-1.0, 1.0], [-1.0, 0.0], [0.0, 0.0], [0.0, 1.0], [-1.0, 1.0]]],
    }

    masked_t1, masked_t2, masked_valid_t1, masked_valid_t2, aoi_mask = _apply_aoi_mask_to_aligned_inputs(
        arr_t1=arr_t1,
        arr_t2=arr_t2,
        t1_valid_mask=t1_valid_mask,
        t2_valid_mask=t2_valid_mask,
        reference_raster_path=reference_path,
        normalized_aoi=aoi,
    )

    assert masked_t1.shape == arr_t1.shape
    assert masked_t2.shape == arr_t2.shape
    assert masked_valid_t1.shape == t1_valid_mask.shape
    assert masked_valid_t2.shape == t2_valid_mask.shape
    assert aoi_mask.shape == t1_valid_mask.shape
    assert np.count_nonzero(masked_valid_t1) < masked_valid_t1.size
    assert np.all(masked_t1[~aoi_mask] == 0)
    assert np.all(masked_t2[~aoi_mask] == 0)
