from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class MaskComparison:
    baseline_positive_pixels: int
    variant_positive_pixels: int
    total_pixels: int
    intersection_pixels: int
    union_pixels: int
    mask_iou: float
    pixel_disagreement_ratio: float
    false_negative_pixels_vs_baseline: int
    false_positive_pixels_vs_baseline: int
    false_negative_ratio_vs_baseline: float
    false_positive_ratio_vs_baseline: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_positive_pixels": self.baseline_positive_pixels,
            "variant_positive_pixels": self.variant_positive_pixels,
            "total_pixels": self.total_pixels,
            "intersection_pixels": self.intersection_pixels,
            "union_pixels": self.union_pixels,
            "mask_iou": self.mask_iou,
            "pixel_disagreement_ratio": self.pixel_disagreement_ratio,
            "false_negative_pixels_vs_baseline": self.false_negative_pixels_vs_baseline,
            "false_positive_pixels_vs_baseline": self.false_positive_pixels_vs_baseline,
            "false_negative_ratio_vs_baseline": self.false_negative_ratio_vs_baseline,
            "false_positive_ratio_vs_baseline": self.false_positive_ratio_vs_baseline,
        }


def _as_bool_mask(value: np.ndarray) -> np.ndarray:
    mask = np.asarray(value)
    if mask.ndim != 2:
        raise ValueError(f"Expected a 2D mask, got shape {mask.shape}.")
    return mask.astype(bool, copy=False)


def compare_binary_masks(baseline: np.ndarray, variant: np.ndarray) -> MaskComparison:
    baseline_mask = _as_bool_mask(baseline)
    variant_mask = _as_bool_mask(variant)
    if baseline_mask.shape != variant_mask.shape:
        raise ValueError(
            f"Mask shapes must match for ablation comparison. Got {baseline_mask.shape} and {variant_mask.shape}."
        )

    intersection = baseline_mask & variant_mask
    union = baseline_mask | variant_mask
    disagreement = baseline_mask != variant_mask
    false_negative = baseline_mask & ~variant_mask
    false_positive = ~baseline_mask & variant_mask

    baseline_positive = int(np.count_nonzero(baseline_mask))
    union_pixels = int(np.count_nonzero(union))
    total_pixels = int(baseline_mask.size)
    return MaskComparison(
        baseline_positive_pixels=baseline_positive,
        variant_positive_pixels=int(np.count_nonzero(variant_mask)),
        total_pixels=total_pixels,
        intersection_pixels=int(np.count_nonzero(intersection)),
        union_pixels=union_pixels,
        mask_iou=(int(np.count_nonzero(intersection)) / union_pixels if union_pixels else 1.0),
        pixel_disagreement_ratio=(int(np.count_nonzero(disagreement)) / total_pixels if total_pixels else 0.0),
        false_negative_pixels_vs_baseline=int(np.count_nonzero(false_negative)),
        false_positive_pixels_vs_baseline=int(np.count_nonzero(false_positive)),
        false_negative_ratio_vs_baseline=(
            int(np.count_nonzero(false_negative)) / baseline_positive if baseline_positive else 0.0
        ),
        false_positive_ratio_vs_baseline=(
            int(np.count_nonzero(false_positive)) / baseline_positive if baseline_positive else 0.0
        ),
    )


def reduction_percent(*, baseline: float, variant: float) -> float:
    if baseline <= 0:
        return 0.0
    return ((baseline - variant) / baseline) * 100.0


def percent_delta(*, baseline: float, variant: float) -> float:
    if baseline == 0:
        return 0.0 if variant == 0 else 100.0
    return ((variant - baseline) / baseline) * 100.0


def is_stride_variant_candidate(
    *,
    mask_iou: float,
    pixel_disagreement_ratio: float,
    false_negative_ratio_vs_baseline: float,
    false_positive_ratio_vs_baseline: float,
    polygon_count_delta_percent: float,
    total_area_delta_percent: float,
    forward_ms_reduction_percent: float,
) -> bool:
    return (
        mask_iou >= 0.995
        and pixel_disagreement_ratio <= 0.002
        and false_negative_ratio_vs_baseline <= 0.002
        and false_positive_ratio_vs_baseline <= 0.002
        and abs(polygon_count_delta_percent) <= 2.0
        and abs(total_area_delta_percent) <= 2.0
        and forward_ms_reduction_percent >= 10.0
    )
