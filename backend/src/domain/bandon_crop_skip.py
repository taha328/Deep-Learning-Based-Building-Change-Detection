from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CropSkipDecision:
    skip: bool
    reason: str | None
    aoi_ratio: float
    valid_inside_aoi_ratio: float
    valid_ratio_within_aoi: float
    aoi_pixels: int
    valid_inside_aoi_pixels: int


def should_skip_crop(
    aoi_mask_crop: np.ndarray,
    valid_pair_mask_crop: np.ndarray,
    min_valid_ratio_within_aoi: float,
    *,
    skip_outside_aoi: bool = True,
    skip_nodata: bool = True,
) -> CropSkipDecision:
    if aoi_mask_crop.shape != valid_pair_mask_crop.shape:
        raise ValueError(
            f"AOI mask crop shape {aoi_mask_crop.shape} does not match valid-pair mask crop shape {valid_pair_mask_crop.shape}."
        )

    aoi_mask = np.asarray(aoi_mask_crop, dtype=bool)
    valid_pair_mask = np.asarray(valid_pair_mask_crop, dtype=bool)
    crop_pixels = int(aoi_mask.size)
    aoi_pixels = int(np.count_nonzero(aoi_mask))
    valid_inside_aoi_pixels = int(np.count_nonzero(aoi_mask & valid_pair_mask))
    aoi_ratio = (aoi_pixels / crop_pixels) if crop_pixels else 0.0
    valid_inside_aoi_ratio = (valid_inside_aoi_pixels / crop_pixels) if crop_pixels else 0.0
    valid_ratio_within_aoi = (valid_inside_aoi_pixels / aoi_pixels) if aoi_pixels else 0.0

    reason: str | None = None
    if skip_outside_aoi and aoi_pixels == 0:
        reason = "outside_aoi"
    elif skip_nodata and valid_inside_aoi_pixels == 0:
        reason = "no_valid_paired_imagery_inside_aoi"
    elif skip_nodata and valid_ratio_within_aoi < float(min_valid_ratio_within_aoi):
        reason = "low_valid_paired_imagery_inside_aoi"

    return CropSkipDecision(
        skip=reason is not None,
        reason=reason,
        aoi_ratio=float(aoi_ratio),
        valid_inside_aoi_ratio=float(valid_inside_aoi_ratio),
        valid_ratio_within_aoi=float(valid_ratio_within_aoi),
        aoi_pixels=aoi_pixels,
        valid_inside_aoi_pixels=valid_inside_aoi_pixels,
    )
