from __future__ import annotations

import json

from src.domain.inference_timing import (
    aggregate_timing_records,
    safe_merge_json_file,
    summary_statistics,
    write_timing_summary,
)


def test_summary_statistics_are_deterministic() -> None:
    stats = summary_statistics([10.0, 20.0, 30.0, 40.0])

    assert stats == {
        "count": 4,
        "mean_ms": 25.0,
        "median_ms": 25.0,
        "p90_ms": 37.0,
        "p95_ms": 38.5,
        "max_ms": 40.0,
        "min_ms": 10.0,
        "total_ms": 100.0,
    }


def test_aggregate_timing_records_preserves_empty_fields() -> None:
    summary = aggregate_timing_records(
        [{"tile_total_wall_ms": 10.0}, {"tile_total_wall_ms": 30.0}],
        ["tile_total_wall_ms", "child_forward_total_ms"],
    )

    assert summary["tile_total_wall_ms"]["mean_ms"] == 20.0
    assert summary["child_forward_total_ms"]["count"] == 0
    assert summary["child_forward_total_ms"]["mean_ms"] is None


def test_safe_merge_json_file_adds_fields_without_removing_existing(tmp_path) -> None:
    path = tmp_path / "run_metadata.json"
    path.write_text(json.dumps({"existing": {"value": 1}}), encoding="utf-8")

    assert safe_merge_json_file(path, {"parent_timing_ms": {"tile_total_wall_ms": 12.5}})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["existing"] == {"value": 1}
    assert payload["parent_timing_ms"]["tile_total_wall_ms"] == 12.5


def test_write_timing_summary_shape(tmp_path) -> None:
    path = tmp_path / "timing_summary.json"

    assert write_timing_summary(
        path,
        run_id="unit-run",
        records=[
            {"tile_total_wall_ms": 10.0, "bandon_subprocess_wall_ms": 8.0},
            {"tile_total_wall_ms": 20.0, "bandon_subprocess_wall_ms": 14.0},
        ],
        fields=["tile_total_wall_ms", "bandon_subprocess_wall_ms"],
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "unit-run"
    assert payload["record_count"] == 2
    assert payload["summary"]["tile_total_wall_ms"]["median_ms"] == 15.0
    assert payload["summary"]["bandon_subprocess_wall_ms"]["max_ms"] == 14.0
