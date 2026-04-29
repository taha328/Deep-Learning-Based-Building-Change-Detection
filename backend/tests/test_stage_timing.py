from __future__ import annotations

import json
import zipfile

import pytest

from src.domain.artifact_manifest import build_manifest, iter_exportable_artifacts, read_manifest, write_manifest_atomic
from src.domain.exports import create_export_bundle_from_manifest
from src.domain.stage_timing import StageTimingRecorder


def test_stage_timing_recorder_writes_success_report(tmp_path) -> None:
    recorder = StageTimingRecorder(run_id="run-1", pipeline_kind="detection", project_id="project-1")

    with recorder.stage("inference", tile_count=4, ignored={"small": "ok"}):
        pass

    report_path = recorder.write_timing_report(tmp_path / "timing.json")
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["run_id"] == "run-1"
    assert payload["project_id"] == "project-1"
    assert payload["pipeline_kind"] == "detection"
    assert payload["total_runtime_ms"] >= 0
    assert payload["stages"][0]["name"] == "inference"
    assert payload["stages"][0]["status"] == "success"
    assert payload["stages"][0]["metadata"]["tile_count"] == 4
    assert payload["summary"]["slowest_stage"] == "inference"


def test_stage_timing_recorder_records_failure_and_reraises() -> None:
    recorder = StageTimingRecorder(run_id="run-2", pipeline_kind="segmentation")

    with pytest.raises(ValueError):
        with recorder.stage("vectorization"):
            raise ValueError("bad geometry")

    payload = recorder.to_dict()
    assert payload["stages"][0]["name"] == "vectorization"
    assert payload["stages"][0]["status"] == "failed"
    assert payload["stages"][0]["error_type"] == "ValueError"


def test_timing_report_is_manifest_metadata_and_not_exportable(tmp_path) -> None:
    request_dir = tmp_path / "runtime_cache" / "requests" / "run-3"
    request_dir.mkdir(parents=True)
    final_path = request_dir / "building_change_blocks.geojson"
    final_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    timing_path = request_dir / "timing.json"
    timing_path.write_text("{}", encoding="utf-8")

    manifest = build_manifest("run-3", request_dir, [])
    write_manifest_atomic(request_dir, manifest)

    loaded = read_manifest(request_dir)
    assert loaded is not None
    entries_by_name = {item["path"].split("/")[-1]: item for item in loaded["artifacts"]}
    assert entries_by_name["timing.json"]["artifact_type"] == "metadata"
    assert entries_by_name["timing.json"]["include_in_export"] is False
    assert iter_exportable_artifacts(request_dir) == [final_path]


def test_explicit_export_records_zip_write_timing_and_excludes_timing_files(tmp_path) -> None:
    request_dir = tmp_path / "runtime_cache" / "requests" / "run-4"
    request_dir.mkdir(parents=True)
    final_path = request_dir / "building_change_blocks.geojson"
    final_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    (request_dir / "timing.json").write_text("{}", encoding="utf-8")
    manifest = build_manifest("run-4", request_dir, [])
    write_manifest_atomic(request_dir, manifest)

    bundle_path = create_export_bundle_from_manifest(request_dir, force=True)

    export_timing = json.loads((request_dir / "export_timing.json").read_text(encoding="utf-8"))
    assert "export_zip_write" in {stage["name"] for stage in export_timing["stages"]}
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())
    assert "building_change_blocks.geojson" in names
    assert "timing.json" not in names
    assert "export_timing.json" not in names
