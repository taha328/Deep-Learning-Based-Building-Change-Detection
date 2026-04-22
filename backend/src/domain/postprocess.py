from __future__ import annotations

import numpy as np
from scipy import ndimage


def dilate_mask(mask: np.ndarray, pixels: int) -> np.ndarray:
    if pixels <= 0:
        return mask.astype(bool)
    structure = np.ones((3, 3), dtype=bool)
    return ndimage.binary_dilation(mask.astype(bool), structure=structure, iterations=int(pixels))


def remove_small_components(mask: np.ndarray, min_pixels: int) -> tuple[np.ndarray, np.ndarray]:
    labeled, num = ndimage.label(mask.astype(np.uint8))
    keep = np.zeros_like(mask, dtype=bool)
    for label_id in range(1, num + 1):
        component = labeled == label_id
        if int(component.sum()) >= min_pixels:
            keep |= component
    kept_labels, _ = ndimage.label(keep.astype(np.uint8))
    return keep, kept_labels


def suppress_edge_hugging_components(
    mask: np.ndarray,
    *,
    reference_mask: np.ndarray,
    min_core_distance_pixels: int,
    min_core_pixels: int = 1,
) -> np.ndarray:
    candidate_mask = mask.astype(bool)
    if min_core_distance_pixels <= 0 or not candidate_mask.any():
        return candidate_mask

    reference = reference_mask.astype(bool)
    distance_outside_reference = ndimage.distance_transform_edt(~reference)
    core_mask = candidate_mask & (distance_outside_reference >= float(min_core_distance_pixels))

    labeled, num = ndimage.label(candidate_mask.astype(np.uint8))
    keep = np.zeros_like(candidate_mask, dtype=bool)
    for label_id in range(1, num + 1):
        component = labeled == label_id
        if int((core_mask & component).sum()) >= max(1, int(min_core_pixels)):
            keep |= component
    return keep
