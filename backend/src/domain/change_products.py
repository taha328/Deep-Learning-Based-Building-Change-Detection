from __future__ import annotations

import numpy as np

from src.domain.postprocess import (
    dilate_mask,
    remove_small_components,
    suppress_edge_hugging_components,
)


def derive_change_probability(t1_building_prob: np.ndarray, t2_building_prob: np.ndarray) -> np.ndarray:
    return np.clip(t2_building_prob - t1_building_prob, 0.0, 1.0).astype(np.float32)


def threshold_change_probability(
    change_probability: np.ndarray,
    *,
    change_threshold: float,
    valid_comparison_mask: np.ndarray | None = None,
) -> np.ndarray:
    mask = change_probability >= change_threshold
    if valid_comparison_mask is not None:
        mask &= valid_comparison_mask.astype(bool)
    return mask


def derive_new_building_products(
    change_prob: np.ndarray,
    t1_building_prob: np.ndarray,
    t2_building_prob: np.ndarray,
    *,
    change_threshold: float,
    semantic_threshold: float,
    min_new_building_pixels: int,
    old_building_mask_dilation_pixels: int,
    new_building_core_distance_pixels: int = 0,
    valid_comparison_mask: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    valid_mask = (
        np.ones(change_prob.shape, dtype=bool)
        if valid_comparison_mask is None
        else valid_comparison_mask.astype(bool)
    )
    change_mask = threshold_change_probability(
        change_prob,
        change_threshold=change_threshold,
        valid_comparison_mask=valid_mask,
    )
    t1_building_mask = (t1_building_prob >= semantic_threshold) & valid_mask
    t2_building_mask = (t2_building_prob >= semantic_threshold) & valid_mask
    t1_building_mask_dilated = dilate_mask(t1_building_mask, old_building_mask_dilation_pixels) & valid_mask
    new_building_mask_raw = change_mask & (~t1_building_mask_dilated) & t2_building_mask
    new_building_mask_filtered = suppress_edge_hugging_components(
        new_building_mask_raw,
        reference_mask=t1_building_mask_dilated,
        min_core_distance_pixels=new_building_core_distance_pixels,
    )
    new_building_mask, new_building_labels = remove_small_components(
        new_building_mask_filtered,
        min_new_building_pixels,
    )
    return {
        "change_mask": change_mask,
        "t1_building_mask": t1_building_mask,
        "t1_building_mask_dilated": t1_building_mask_dilated,
        "t2_building_mask": t2_building_mask,
        "new_building_mask_raw": new_building_mask_raw,
        "new_building_mask_filtered": new_building_mask_filtered,
        "new_building_mask": new_building_mask,
        "new_building_labels": new_building_labels,
    }
