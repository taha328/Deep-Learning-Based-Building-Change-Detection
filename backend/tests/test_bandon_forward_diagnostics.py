from __future__ import annotations

import json

import numpy as np
import torch

from src.domain.bandon_forward_diagnostics import (
    build_crop_summary,
    build_forward_diagnostics,
    build_model_load_diagnostics,
    build_slide_crop_bounds,
    current_torch_mode_flags,
)


class DummyModel(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.weight = torch.nn.Parameter(torch.zeros(1, dtype=torch.float32))
        self.cfg = type(
            "Cfg",
            (),
            {"test_cfg": {"mode": "slide", "crop_size": (16, 16), "stride": (8, 8)}},
        )()


def _forward_metadata(*, crop_summaries=None, coverage_counts=None, **overrides):
    model = DummyModel()
    input_tensor = torch.full((1, 6, 32, 48), 7.1234, dtype=torch.float32)
    result = [[np.full((2, 32, 48), 8.5678, dtype=np.float32)]]
    metadata = build_forward_diagnostics(
        input_tensor=input_tensor,
        model=model,
        result=result,
        device=torch.device("cpu"),
        device_configured="cpu",
        aoi_height=32,
        aoi_width=48,
        no_grad_active=True,
        inference_mode_active=False,
        mps_synchronize_used=False,
        cpu_to_mps_transfer_count=0,
        mps_to_cpu_transfer_count=0,
        model_reload_count_this_job=1,
        model_reused=False,
        mps_available=False,
        mps_built=False,
        crop_summaries=crop_summaries,
        coverage_counts=coverage_counts,
    )
    metadata.update(overrides)
    return metadata, result


def _make_crop_summaries(
    *,
    durations: list[float],
    input_size: tuple[int, int] = (16, 16),
    bounds: list[dict[str, int]] | None = None,
    output_change_pixels: list[int] | None = None,
) -> tuple[list[dict[str, object]], np.ndarray]:
    base_bounds = bounds or build_slide_crop_bounds(
        input_height=32,
        input_width=48,
        crop_height=16,
        crop_width=16,
        stride_height=8,
        stride_width=8,
    )
    crop_bounds = [base_bounds[index % len(base_bounds)] for index in range(len(durations))]
    summaries: list[dict[str, object]] = []
    coverage_counts = np.zeros((32, 48), dtype=np.uint16)
    for index, duration in enumerate(durations):
        crop_bound = crop_bounds[index]
        height, width = input_size
        input_tensor = torch.zeros((1, 6, height, width), dtype=torch.float32)
        output_tensor = torch.zeros((1, 2, height, width), dtype=torch.float32)
        change_pixels = output_change_pixels[index] if output_change_pixels is not None else index + 1
        if change_pixels > 0:
            flat = output_tensor[0, 1].reshape(-1)
            flat[:change_pixels] = 1.0
        x0 = int(crop_bound["x0"])
        y0 = int(crop_bound["y0"])
        x1 = int(crop_bound["x1"])
        y1 = int(crop_bound["y1"])
        coverage_before_pixels = int(np.count_nonzero(coverage_counts[y0:y1, x0:x1]))
        summaries.append(
            build_crop_summary(
                index=index,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                duration_ms=duration,
                input_tensor=input_tensor,
                output_tensor=output_tensor,
                previous_bounds=(crop_bounds[index - 1]["x0"], crop_bounds[index - 1]["y0"], crop_bounds[index - 1]["x1"], crop_bounds[index - 1]["y1"]) if index > 0 else None,
                coverage_before_pixels=coverage_before_pixels,
            )
        )
        coverage_counts[y0:y1, x0:x1] += 1
    return summaries, coverage_counts


def test_bandon_forward_metadata_records_input_shape_dtype_device() -> None:
    metadata, _ = _forward_metadata()

    assert metadata["input_tensor_shapes"] == [[1, 6, 32, 48]]
    assert metadata["input_tensor_dtypes"] == ["torch.float32"]
    assert metadata["input_tensor_devices_before_forward"] == ["cpu"]
    assert metadata["model_parameter_devices"] == ["cpu"]
    assert metadata["model_parameter_dtypes"] == ["torch.float32"]
    assert metadata["output_tensor_shapes"] == [[2, 32, 48]]
    assert metadata["output_tensor_dtypes"] == ["float32"]
    assert metadata["output_tensor_devices_after_forward"] == ["cpu"]
    assert metadata["input_width"] == 48
    assert metadata["input_height"] == 32
    assert metadata["model_input_width"] == 48
    assert metadata["model_input_height"] == 32
    assert metadata["aoi_pixel_width"] == 48
    assert metadata["aoi_pixel_height"] == 32


def test_bandon_forward_metadata_records_grad_and_inference_mode_state(monkeypatch) -> None:
    monkeypatch.setattr(torch, "is_grad_enabled", lambda: False)
    monkeypatch.setattr(torch, "is_inference_mode_enabled", lambda: True)

    flags = current_torch_mode_flags()

    assert flags["no_grad_active"] is True
    assert flags["inference_mode_active"] is True


def test_bandon_forward_metadata_records_crop_and_forward_counts() -> None:
    metadata, _ = _forward_metadata()

    assert metadata["crop_count"] == 15
    assert metadata["tile_count"] == 15
    assert metadata["forward_call_count"] == 1
    assert metadata["runs_multiple_crops"] is True


def test_per_crop_diagnostics_records_crop_count_and_forward_call_count() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[10.0, 12.0, 11.0, 13.0])
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    assert metadata["crop_count"] == 4
    assert metadata["forward_call_count"] == 4
    assert metadata["tile_count"] == 4
    assert len(metadata["crop_summaries"]) == 4


def test_per_crop_diagnostics_records_duration_stats() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[10.0, 12.0, 11.0, 13.0])
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    assert metadata["mean_crop_ms"] == 11.5
    assert metadata["median_crop_ms"] == 11.5
    assert round(metadata["p95_crop_ms"], 2) == 12.85
    assert metadata["slowest_crop_index"] == 3
    assert metadata["fastest_crop_index"] == 0
    assert metadata["crop_duration_cv"] > 0


def test_per_crop_diagnostics_records_crop_coordinates() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[10.0, 10.0])
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    first = metadata["crop_summaries"][0]
    assert first["index"] == 0
    assert first["x0"] == 0
    assert first["y0"] == 0
    assert first["x1"] == 16
    assert first["y1"] == 16
    assert first["width"] == 16
    assert first["height"] == 16


def test_per_crop_diagnostics_records_padding_and_valid_ratios() -> None:
    bounds = [{"index": 0, "x0": 0, "y0": 0, "x1": 12, "y1": 12, "width": 12, "height": 12}]
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[10.0], bounds=bounds)
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    summary = metadata["crop_summaries"][0]
    assert summary["padding_pixels"] == 112
    assert summary["padding_ratio"] == 112 / 256
    assert summary["valid_pixels"] == 144
    assert summary["valid_ratio"] == 144 / 256


def test_per_crop_diagnostics_records_output_contribution_summary() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(
        durations=[10.0, 10.0, 10.0],
        output_change_pixels=[0, 3, 6],
    )
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    assert metadata["empty_or_low_contribution_crop_count"] >= 1
    assert metadata["crop_summaries"][0]["output_nonzero_pixels"] == 0
    assert metadata["crop_summaries"][1]["output_nonzero_pixels"] == 3
    assert metadata["crop_summaries"][2]["output_nonzero_pixels"] == 6


def test_per_crop_diagnostics_records_stride_overlap_metadata() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[10.0, 10.0, 10.0, 10.0])
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    assert metadata["crop_size"] == 16
    assert metadata["stride"] == 8
    assert metadata["overlap_pixels"] == 8
    assert metadata["overlap_ratio"] == 0.5
    assert metadata["duplicate_coverage_ratio"] > 0.0
    assert metadata["high_overlap_crop_count"] >= 1


def test_per_crop_diagnostics_records_uniformity_stats() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[10.0, 10.0, 10.0, 10.0])
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    assert metadata["crop_duration_cv"] == 0.0
    assert metadata["uniform_crop_cost"] is True
    assert metadata["slowest_to_median_ratio"] == 1.0
    assert metadata["p95_to_median_ratio"] == 1.0


def test_per_crop_diagnostics_limits_metadata_size() -> None:
    crop_summaries, coverage_counts = _make_crop_summaries(durations=[float(i + 1) for i in range(101)])
    metadata, _ = _forward_metadata(crop_summaries=crop_summaries, coverage_counts=coverage_counts)

    assert metadata["crop_summaries"] is None
    assert len(metadata["top_slowest_crop_summaries"]) == 20
    assert len(metadata["top_high_padding_crop_summaries"]) == 20
    assert len(metadata["top_low_contribution_crop_summaries"]) == 20


def test_bandon_forward_metadata_records_model_reuse_flag() -> None:
    model = DummyModel()
    metadata = build_model_load_diagnostics(
        model=model,
        device=torch.device("cpu"),
        device_configured="cpu",
        model_reload_count_this_job=1,
        model_reused=False,
        no_grad_active=False,
        inference_mode_active=False,
        checkpoint_path="/tmp/checkpoint.pth",
        process_id=12345,
        mps_memory={},
    )

    assert metadata["model_reload_count_this_job"] == 1
    assert metadata["model_reused"] is False
    assert metadata["model_cached_or_reused"] is False
    assert metadata["process_id"] == 12345
    assert metadata["checkpoint_path"] == "/tmp/checkpoint.pth"
    assert metadata["count"] == 1


def test_bandon_forward_metadata_records_transfer_counts() -> None:
    metadata, _ = _forward_metadata(
        cpu_to_mps_transfer_count=2,
        mps_to_cpu_transfer_count=0,
        transfer_sites=["build_input_tensor.to(device)", "model.to(device)"],
    )

    assert metadata["cpu_to_mps_transfer_count"] == 2
    assert metadata["mps_to_cpu_transfer_count"] == 0
    assert metadata["transfer_sites"] == ["build_input_tensor.to(device)", "model.to(device)"]
    assert metadata["mps_synchronize_used"] is False


def test_bandon_forward_metadata_does_not_include_tensor_values() -> None:
    metadata, _ = _forward_metadata()
    payload = json.dumps(metadata, sort_keys=True)

    assert "7.1234" not in payload
    assert "8.5678" not in payload


def test_bandon_forward_metadata_does_not_include_secrets() -> None:
    metadata, _ = _forward_metadata()
    payload = json.dumps(metadata, sort_keys=True)

    assert "MAPBOX_ACCESS_TOKEN" not in payload
    assert "token" not in payload.lower()


def test_forward_diagnostics_do_not_change_outputs() -> None:
    metadata, result = _forward_metadata()
    before = np.array(result[0][0], copy=True)

    assert metadata["cpu_fallback_observed"] is False
    assert np.array_equal(before, result[0][0])


def test_forward_diagnostics_timing_json_non_exportable(tmp_path) -> None:
    from src.domain.artifact_manifest import build_manifest, iter_exportable_artifacts, write_manifest_atomic
    from src.domain.stage_timing import StageTimingRecorder

    request_dir = tmp_path / "runtime_cache" / "requests" / "run-1"
    request_dir.mkdir(parents=True)
    (request_dir / "building_change_blocks.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    recorder = StageTimingRecorder(run_id="run-1", pipeline_kind="detection")
    with recorder.stage("forward", input_tensor_shapes=[[1, 6, 32, 48]], input_tensor_devices_before_forward=["cpu"]):
        pass
    recorder.write_timing_report(request_dir / "timing.json")

    manifest = build_manifest("run-1", request_dir, [])
    write_manifest_atomic(request_dir, manifest)

    loaded = iter_exportable_artifacts(request_dir)
    assert (request_dir / "building_change_blocks.geojson") in loaded
    assert (request_dir / "timing.json") not in loaded
