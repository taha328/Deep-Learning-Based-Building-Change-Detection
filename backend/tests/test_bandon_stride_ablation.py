from __future__ import annotations

import numpy as np
from datetime import date

from src.config import Settings
from src.domain.bandon_runner import _clean_env
from src.domain.bandon_stride_ablation import (
    compare_binary_masks,
    is_stride_variant_candidate,
    reduction_percent,
)
from src.domain.wayback import WaybackRelease
from src.schemas import RunRequest
from src.services.validation import validate_request


def test_mask_iou_computation() -> None:
    baseline = np.array([[1, 1], [0, 0]], dtype=np.uint8)
    variant = np.array([[1, 0], [1, 0]], dtype=np.uint8)

    comparison = compare_binary_masks(baseline, variant)

    assert comparison.intersection_pixels == 1
    assert comparison.union_pixels == 3
    assert comparison.mask_iou == 1 / 3


def test_pixel_disagreement_computation() -> None:
    baseline = np.array([[1, 1], [0, 0]], dtype=np.uint8)
    variant = np.array([[1, 0], [1, 0]], dtype=np.uint8)

    comparison = compare_binary_masks(baseline, variant)

    assert comparison.pixel_disagreement_ratio == 2 / 4


def test_false_positive_negative_computation() -> None:
    baseline = np.array([[1, 1], [0, 0]], dtype=np.uint8)
    variant = np.array([[1, 0], [1, 0]], dtype=np.uint8)

    comparison = compare_binary_masks(baseline, variant)

    assert comparison.false_negative_pixels_vs_baseline == 1
    assert comparison.false_positive_pixels_vs_baseline == 1
    assert comparison.false_negative_ratio_vs_baseline == 1 / 2
    assert comparison.false_positive_ratio_vs_baseline == 1 / 2


def test_stride_variant_candidate_thresholds() -> None:
    assert is_stride_variant_candidate(
        mask_iou=0.996,
        pixel_disagreement_ratio=0.001,
        false_negative_ratio_vs_baseline=0.001,
        false_positive_ratio_vs_baseline=0.001,
        polygon_count_delta_percent=1.0,
        total_area_delta_percent=1.0,
        forward_ms_reduction_percent=12.0,
    )
    assert not is_stride_variant_candidate(
        mask_iou=0.996,
        pixel_disagreement_ratio=0.001,
        false_negative_ratio_vs_baseline=0.001,
        false_positive_ratio_vs_baseline=0.001,
        polygon_count_delta_percent=1.0,
        total_area_delta_percent=1.0,
        forward_ms_reduction_percent=9.9,
    )


def test_runtime_reduction_percent() -> None:
    assert reduction_percent(baseline=100.0, variant=80.0) == 20.0
    assert reduction_percent(baseline=0.0, variant=80.0) == 0.0


def test_ablation_override_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BANDON_ABLATION_STRIDE", raising=False)

    env = _clean_env()

    assert "BANDON_ABLATION_STRIDE" not in env


def test_stride_variant_env_override_is_passed_to_bandon_subprocess(monkeypatch) -> None:
    monkeypatch.setenv("BANDON_ABLATION_STRIDE", "400")

    env = _clean_env()

    assert env["BANDON_ABLATION_STRIDE"] == "400"


def test_ablation_override_does_not_change_default_request_hash(monkeypatch) -> None:
    releases = [
        WaybackRelease(
            identifier="WB_2025_R12",
            release_date=date(2025, 12, 1),
            label="WB_2025_R12",
            release_num=12,
            tile_matrix_sets=("default028mm",),
            resource_url_template="https://example.invalid/2025/{z}/{y}/{x}",
        ),
        WaybackRelease(
            identifier="WB_2026_R04",
            release_date=date(2026, 4, 1),
            label="WB_2026_R04",
            release_num=4,
            tile_matrix_sets=("default028mm",),
            resource_url_template="https://example.invalid/2026/{z}/{y}/{x}",
        ),
    ]
    request = RunRequest(
        mode="fast_preview",
        t1_release="WB_2025_R12",
        t2_release="WB_2026_R04",
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [
                [
                    [-7.0, 33.0],
                    [-6.999, 33.0],
                    [-6.999, 33.001],
                    [-7.0, 33.001],
                    [-7.0, 33.0],
                ]
            ],
        },
    )
    settings = Settings(model_backend_default="bandon_mps")
    monkeypatch.delenv("BANDON_ABLATION_STRIDE", raising=False)
    _validation, prepared_default = validate_request(
        request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )
    monkeypatch.setenv("BANDON_ABLATION_STRIDE", "400")
    _validation, prepared_with_env = validate_request(
        request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert prepared_default is not None
    assert prepared_with_env is not None
    assert prepared_default.request_hash == prepared_with_env.request_hash
