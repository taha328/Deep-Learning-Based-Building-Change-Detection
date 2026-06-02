from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_runtime_storage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("cleanup_runtime_storage", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_file(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _age(path: Path, *, hours: int) -> None:
    timestamp = time.time() - (hours * 3600)
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)
    if path.is_dir():
        for child in path.rglob("*"):
            os.utime(child, (timestamp, timestamp), follow_symlinks=False)
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def _make_runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime_cache"
    referenced = runtime / "requests" / "run-referenced"
    artifact_referenced = runtime / "requests" / "run-artifact-ref"
    artifact_path_referenced = runtime / "requests" / "run-artifact-path"
    reference_imagery_referenced = runtime / "requests" / "run-reference-imagery"
    orphan = runtime / "requests" / "run-orphan"
    active = runtime / "requests" / "run-active"
    unknown = runtime / "requests" / "run-unknown"
    for request in (referenced, artifact_referenced, artifact_path_referenced, reference_imagery_referenced, orphan, active, unknown):
        request.mkdir(parents=True)
    _write_json(referenced / "run_response.json", {})
    _write_json(referenced / "manifest.json", {})
    _write_file(referenced / "export_bundle.zip", b"referenced")
    _write_json(artifact_referenced / "run_response.json", {})
    _write_file(artifact_referenced / "export_bundle.zip", b"artifact-ref")
    _write_json(artifact_path_referenced / "run_response.json", {})
    _write_file(artifact_path_referenced / "building_change_polygons.geojson", b"{}")
    _write_json(reference_imagery_referenced / "run_response.json", {})
    _write_file(reference_imagery_referenced / "reference_imagery_cog.tif", b"cog")
    _write_file(reference_imagery_referenced / "t2_preview.png", b"png")
    _write_json(orphan / "run_response.json", {})
    _write_file(orphan / "export_bundle.zip", b"zip")
    _write_file(orphan / "tiles" / "0" / "0" / "0.pbf", b"tile")
    _write_file(orphan / "tmp" / "scratch.bin", b"tmp")
    _write_json(active / "run_response.json", {})
    _write_file(active / "export_bundle.zip", b"active")
    _write_file(unknown / "export_bundle.zip", b"unknown")
    for request in (referenced, artifact_referenced, artifact_path_referenced, reference_imagery_referenced, orphan, unknown):
        _age(request, hours=120)

    project_dir = runtime / "temporal_projects" / "temporal-test"
    project_artifact = project_dir / "milestones" / "WB_2026_R04" / "additions.geojson"
    _write_file(project_artifact, b"{}")
    _write_json(
        project_dir / "project.json",
        {
            "project_id": "temporal-test",
            "project_dir": str(project_dir),
            "download_bundle_path": str(artifact_referenced / "export_bundle.zip"),
            "milestones": [
                {
                    "release_identifier": "WB_2026_R04",
                    "pair_request_hash": "run-referenced",
                    "reference_imagery": {
                        "cog_path": str(reference_imagery_referenced / "reference_imagery_cog.tif"),
                        "image_path": str(reference_imagery_referenced / "t2_preview.png"),
                    },
                    "artifacts": [
                        {"key": "additions", "path": str(project_artifact)},
                        {"key": "change_polygons", "path": str(artifact_path_referenced / "building_change_polygons.geojson")},
                    ],
                }
            ],
        },
    )

    _write_file(runtime / "tmp" / "old-tmp" / "scratch.bin", b"tmp")
    _age(runtime / "tmp" / "old-tmp", hours=120)
    for folder in ("reference_tiles", "temporal_vector_tiles", "qgis_artifacts"):
        _write_file(runtime / folder / "entry" / "cache.bin", b"cache")
        _age(runtime / folder / "entry", hours=120)
    _write_file(runtime / "dev_client_logs" / "client.ndjson", b"log")
    for folder in ("wayback_mosaics", "imagery_cache", "temporal_projects", "db_payloads"):
        _write_file(runtime / folder / "protected" / "sentinel.bin", b"protected")
    return runtime


def _file_snapshot(root: Path) -> set[tuple[str, int]]:
    return {(str(path.relative_to(root)), path.stat().st_size) for path in root.rglob("*") if path.is_file()}


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime(tmp_path)
    before = _file_snapshot(runtime)

    report = module.build_report(
        runtime,
        apply=False,
        yes=False,
        older_than_hours=72,
        active_window_hours=24,
        max_rows=100,
    )

    assert report["mode"] == "dry_run"
    assert _file_snapshot(runtime) == before


def test_apply_without_yes_deletes_nothing_and_reports_error(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime(tmp_path)
    before = _file_snapshot(runtime)

    report = module.build_report(
        runtime,
        apply=True,
        yes=False,
        older_than_hours=72,
        active_window_hours=24,
        max_rows=100,
    )

    assert any(error["error"] == "apply_requires_yes" for error in report["errors"])
    assert _file_snapshot(runtime) == before


def test_pair_request_hash_only_request_becomes_orphan_candidate_after_ttl(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    referenced = next(item for item in report["orphan_request_candidates"] if item["request_hash"] == "run-referenced")
    assert "pair_hash_is_provenance_not_storage_dependency" in referenced["reason"]
    assert referenced["source_references"][0]["reason"] == "pair_request_hash_reference"
    assert referenced["source_references"][0]["field_path"].endswith("milestones[0].pair_request_hash")


def test_request_folder_referenced_by_download_bundle_path_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    referenced = next(item for item in report["protected_requests"] if item["request_hash"] == "run-artifact-ref")
    assert "download_bundle_reference" in referenced["reason"]
    assert referenced["protection_reasons"][0]["reason"] == "download_bundle_reference"
    assert referenced["protection_reasons"][0]["field_path"] == "project.json.download_bundle_path"


def test_request_folder_referenced_by_artifact_path_is_protected_with_source(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    referenced = next(item for item in report["protected_requests"] if item["request_hash"] == "run-artifact-path")
    assert "artifact_path_reference" in referenced["reason"]
    source = next(item for item in referenced["protection_reasons"] if item["reason"] == "artifact_path_reference")
    assert source["field_path"] == "project.json.milestones[0].artifacts[1].path"
    assert source["value"].endswith("run-artifact-path/building_change_polygons.geojson")


def test_request_folder_referenced_by_reference_imagery_source_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    referenced = next(item for item in report["protected_requests"] if item["request_hash"] == "run-reference-imagery")
    assert "reference_imagery_source_reference" in referenced["reason"]
    source = next(item for item in referenced["protection_reasons"] if item["reason"] == "reference_imagery_source_reference")
    assert source["field_path"] == "project.json.milestones[0].reference_imagery.cog_path"


def test_protection_reason_counts_are_reported(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    counts = report["protection_reason_counts"]
    assert counts["pair_request_hash_reference"] == 1
    assert counts["artifact_path_reference"] == 1
    assert counts["download_bundle_reference"] == 1
    assert counts["reference_imagery_source_reference"] == 2


def test_active_recent_request_folder_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    active = next(item for item in report["protected_requests"] if item["request_hash"] == "run-active")
    assert "recent_or_active_request" in active["reason"]


def test_unknown_risk_request_folder_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    unknown = next(item for item in report["protected_requests"] if item["request_hash"] == "run-unknown")
    assert "incomplete_request" in unknown["reason"]
    assert any("run-unknown" in item["path"] for item in report["unknown_risk_items"])


def test_old_unreferenced_request_folder_is_reported_as_orphan_candidate(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    orphan = next(item for item in report["orphan_request_candidates"] if item["request_hash"] == "run-orphan")
    assert orphan["reason"] == "old_unreferenced_completed_request"


def test_old_unreferenced_request_artifacts_deleted_only_with_apply_yes(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime(tmp_path)
    export_bundle = runtime / "requests" / "run-orphan" / "export_bundle.zip"
    tiles = runtime / "requests" / "run-orphan" / "tiles"

    dry_report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)
    assert export_bundle.exists()
    assert tiles.exists()
    assert any(item["request_hash"] == "run-orphan" for item in dry_report["cleanup_candidates"])

    apply_report = module.build_report(runtime, apply=True, yes=True, older_than_hours=72, active_window_hours=24, max_rows=100)
    assert apply_report["actions_taken"]
    assert not export_bundle.exists()
    assert not tiles.exists()


def test_export_bundle_and_tiles_are_cleanup_candidates_for_old_orphan_request(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)
    classes = {item["path_class"] for item in report["cleanup_candidates"] if item["request_hash"] == "run-orphan"}

    assert "orphan_export_bundle" in classes
    assert "orphan_tiles_directory" in classes


def test_forbidden_cache_areas_are_never_deleted(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime(tmp_path)

    module.build_report(runtime, apply=True, yes=True, older_than_hours=72, active_window_hours=24, max_rows=100)

    for folder in ("wayback_mosaics", "imagery_cache", "temporal_projects", "db_payloads"):
        assert (runtime / folder / "protected" / "sentinel.bin").is_file()


@pytest.mark.parametrize("folder", ["reference_tiles", "temporal_vector_tiles", "qgis_artifacts"])
def test_derived_cache_folders_are_classified_as_candidates(tmp_path: Path, folder: str) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)

    assert any(item["path_class"] == f"derived_{folder}_entry" for item in report["derived_cache_candidates"])


def test_json_output_is_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_module()
    runtime = _make_runtime(tmp_path)

    assert module.main(["--runtime-cache-dir", str(runtime), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "dry_run"
    assert "cleanup_candidates" in payload
    assert "protection_reason_counts" in payload


def test_markdown_output_contains_required_sections(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)
    markdown = module.render_markdown(report, max_rows=100)

    for section in module.REQUIRED_MARKDOWN_SECTIONS:
        assert f"## {section}" in markdown


def test_apply_mode_reports_actions_taken(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_make_runtime(tmp_path), apply=True, yes=True, older_than_hours=72, active_window_hours=24, max_rows=100)

    assert report["actions_taken"]


def test_cleanup_errors_are_reported_without_deleting_protected_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    runtime = _make_runtime(tmp_path)

    def fail_rmtree(_path: Path) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(module.shutil, "rmtree", fail_rmtree)
    report = module.build_report(runtime, apply=True, yes=True, older_than_hours=72, active_window_hours=24, max_rows=100)

    assert report["errors"]
    assert (runtime / "requests" / "run-artifact-ref" / "export_bundle.zip").is_file()
