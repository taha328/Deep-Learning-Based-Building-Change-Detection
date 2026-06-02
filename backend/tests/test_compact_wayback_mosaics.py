from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from src.domain.reference_imagery_cache import (
    build_reference_imagery_cache_metadata,
    build_reference_imagery_cache_key_payload,
    build_reference_imagery_key,
    reference_imagery_cache_cog_path,
    reference_imagery_cache_metadata_path,
    write_reference_imagery_cache_metadata,
)
from src.services.temporal_reference_imagery import REFERENCE_COG_FORMAT_VERSION, ensure_reference_imagery_cog


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compact_wayback_mosaics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("compact_wayback_mosaics", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_file(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _age(path: Path, *, hours: int = 120) -> None:
    timestamp = time.time() - hours * 3600
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)
    if path.is_dir():
        for child in path.rglob("*"):
            os.utime(child, (timestamp, timestamp), follow_symlinks=False)
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def _write_rgb(path: Path, *, bounds=(0.0, 0.0, 256.0, 256.0), width=256, height=256, crs="EPSG:3857") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=3,
        dtype="uint8",
        crs=crs,
        transform=from_bounds(*bounds, width=width, height=height),
    ) as dst:
        dst.write(np.ones((3, height, width), dtype=np.uint8))
    return path


def _write_mask(path: Path, *, bounds=(0.0, 0.0, 256.0, 256.0), width=256, height=256) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(*bounds, width=width, height=height),
    ) as dst:
        dst.write(np.ones((height, width), dtype=np.uint8), 1)
    return path


def _mosaic_entry(runtime: Path, cache_key: str, *, age: bool = True) -> Path:
    folder = runtime / "wayback_mosaics" / cache_key
    _write_file(folder / "mosaic.tif", b"mosaic-tif")
    _write_file(folder / "mosaic.png", b"mosaic-png")
    _write_json(
        folder / "metadata.json",
        {
            "release_identifier": "WB_2026_R04",
            "release_num": 4,
            "tile_matrix_set": "default028mm",
            "zoom": 19,
            "tile_range": [0, 0, 0, 0],
            "bounds_3857": [0.0, 0.0, 256.0, 256.0],
        },
    )
    _write_file(folder / "valid_mask.tif", b"mask")
    if age:
        _age(folder)
    return folder


def _canonical_for(runtime: Path, cache_key: str, *, valid: bool = True) -> tuple[str, Path]:
    payload = build_reference_imagery_cache_key_payload(
        provider="esri_wayback",
        release_identifier="WB_2026_R04",
        release_num=4,
        tile_matrix_set="default028mm",
        zoom=19,
        tile_range=[0, 0, 0, 0],
        bounds_3857=[0.0, 0.0, 256.0, 256.0],
        source_raster_path=None,
        valid_mask_path=None,
        aoi_hash=cache_key,
        reference_cog_format_version=REFERENCE_COG_FORMAT_VERSION,
    )
    reference_key = build_reference_imagery_key(payload)
    canonical = reference_imagery_cache_cog_path(runtime / "imagery_cache", reference_key)
    metadata_path = reference_imagery_cache_metadata_path(runtime / "imagery_cache", reference_key)
    if valid:
        source = _write_rgb(runtime / "source" / f"{cache_key}.tif")
        mask = _write_mask(runtime / "source" / f"{cache_key}_mask.tif")
        ensure_reference_imagery_cog(source, canonical, valid_mask_path=mask, release_identifier="WB_2026_R04")
        _write_mask(canonical.with_name("valid_mask.tif"))
    else:
        _write_file(canonical, b"not-a-tif")
        _write_mask(canonical.with_name("valid_mask.tif"))
    metadata = build_reference_imagery_cache_metadata(
        reference_imagery_key=reference_key,
        key_payload=payload,
        canonical_cog_path=canonical if canonical.exists() else _write_file(canonical, b"missing"),
    )
    metadata["source_wayback_mosaic_cache_key"] = cache_key
    write_reference_imagery_cache_metadata(metadata_path, metadata)
    return reference_key, canonical


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime_cache"
    _mosaic_entry(runtime, "safe")
    _canonical_for(runtime, "safe")
    _mosaic_entry(runtime, "missing-canonical")
    _mosaic_entry(runtime, "invalid-canonical")
    _canonical_for(runtime, "invalid-canonical", valid=False)
    _mosaic_entry(runtime, "referenced-request")
    _canonical_for(runtime, "referenced-request")
    _write_json(runtime / "requests" / "run-1" / "manifest.json", {"path": str(runtime / "wayback_mosaics" / "referenced-request" / "mosaic.tif")})
    _mosaic_entry(runtime, "referenced-project")
    _canonical_for(runtime, "referenced-project")
    _write_json(runtime / "temporal_projects" / "project" / "project.json", {"path": str(runtime / "wayback_mosaics" / "referenced-project" / "mosaic.png")})
    _mosaic_entry(runtime, "recent", age=False)
    _canonical_for(runtime, "recent")
    for folder in ("imagery_cache", "temporal_projects", "requests", "db_payloads", "wayback_tiles"):
        _write_file(runtime / folder / "sentinel" / "keep.bin", b"keep")
    return runtime


def _force_unknown_for_paths(module, monkeypatch: pytest.MonkeyPatch, marker: str = "large") -> None:
    original = module.read_json_if_small

    def _read_json_if_small(path: Path):
        if marker in path.parts:
            return None, "unknown_metadata"
        return original(path)

    monkeypatch.setattr(module, "read_json_if_small", _read_json_if_small)


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _runtime(tmp_path)
    before = {str(path.relative_to(runtime)) for path in runtime.rglob("*") if path.is_file()}

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    assert report["mode"] == "dry_run"
    assert {str(path.relative_to(runtime)) for path in runtime.rglob("*") if path.is_file()} == before


def test_apply_without_yes_deletes_nothing_and_reports_error(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _runtime(tmp_path)

    report = module.build_report(runtime, apply=True, yes=False, older_than_hours=72, max_rows=100)

    assert any(error["error"] == "apply_requires_yes" for error in report["errors"])
    assert (runtime / "wayback_mosaics" / "safe" / "mosaic.tif").is_file()


def test_mosaic_without_canonical_cog_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)
    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "missing-canonical")

    assert "missing_canonical_cog" in item["reasons"]
    assert "missing_canonical_cog" in item["protection_reasons"]
    assert item["release_identifier"] == "WB_2026_R04"
    assert item["mosaic_tif_path"].endswith("mosaic.tif")
    assert item["metadata_path"].endswith("metadata.json")


def test_mosaic_with_invalid_cog_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)
    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "invalid-canonical")

    assert "canonical_cog_validation_failed" in item["reasons"]


def test_mosaic_referenced_by_request_or_project_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)
    by_key = {item["cache_key"]: item for item in report["protected_mosaics"]}

    assert "referenced_by_request_metadata" in by_key["referenced-request"]["reasons"]
    assert "referenced_by_project_metadata" in by_key["referenced-project"]["reasons"]
    assert by_key["referenced-request"]["source_references"][0]["kind"] == "request"
    assert by_key["referenced-project"]["source_references"][0]["kind"] == "project"


def test_mosaic_missing_tile_range_reports_exact_reason(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    folder = _mosaic_entry(runtime, "missing-tile-range")
    metadata = json.loads((folder / "metadata.json").read_text())
    metadata.pop("tile_range")
    (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    _canonical_for(runtime, "missing-tile-range")

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)
    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "missing-tile-range")

    assert "missing_tile_range" in item["protection_reasons"]
    assert item["missing_metadata_fields"] == ["missing_tile_range"]


def test_protection_reason_counts_are_reported(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)

    counts = report["summary"]["protection_reason_counts"]
    assert counts["missing_canonical_cog"] == 1
    assert counts["referenced_by_request_metadata"] == 1
    assert counts["referenced_by_project_metadata"] == 1


def test_backup_metadata_files_are_ignored_as_active_references(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _mosaic_entry(runtime, "backup-only")
    _canonical_for(runtime, "backup-only")
    _write_json(runtime / "temporal_projects" / "project" / "backups" / "project.json", {"path": str(runtime / "wayback_mosaics" / "backup-only" / "mosaic.tif")})

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    assert any(item["cache_key"] == "backup-only" for item in report["cleanup_candidates"])


def test_ambiguous_metadata_link_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _mosaic_entry(runtime, "ambiguous")
    _canonical_for(runtime, "ambiguous")
    _canonical_for(runtime, "ambiguous-alt")
    metadata_path = sorted((runtime / "imagery_cache").glob("*/metadata.json"))[-1]
    metadata = json.loads(metadata_path.read_text())
    metadata["source_wayback_mosaic_cache_key"] = "ambiguous"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)
    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "ambiguous")

    assert "ambiguous_metadata_link" in item["protection_reasons"]


def test_recent_mosaic_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)
    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "recent")

    assert "ttl_not_met" in item["reasons"]


def test_safe_old_mosaic_is_cleanup_candidate(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)
    item = next(item for item in report["cleanup_candidates"] if item["cache_key"] == "safe")

    assert str(Path(item["path"]) / "mosaic.tif") in item["delete_targets"]
    assert str(Path(item["path"]) / "mosaic.png") in item["delete_targets"]
    assert item["canonical_validation_status"] == "valid"
    assert item["canonical_reference_imagery_key"]


def test_backfilled_metadata_source_wayback_cache_key_makes_mosaic_candidate(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _mosaic_entry(runtime, "backfilled")
    _canonical_for(runtime, "backfilled")
    metadata_path = next((runtime / "imagery_cache").glob("*/metadata.json"))
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("source_wayback_mosaic_cache_key", None)
    metadata["source_wayback_cache_key"] = "backfilled"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    assert any(item["cache_key"] == "backfilled" for item in report["cleanup_candidates"])


def test_apply_deletes_only_allowed_files_and_retains_metadata_and_mask(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _runtime(tmp_path)

    report = module.build_report(runtime, apply=True, yes=True, older_than_hours=72, max_rows=100)

    assert report["actions_taken"]
    assert not (runtime / "wayback_mosaics" / "safe" / "mosaic.tif").exists()
    assert not (runtime / "wayback_mosaics" / "safe" / "mosaic.png").exists()
    assert (runtime / "wayback_mosaics" / "safe" / "metadata.json").is_file()
    assert (runtime / "wayback_mosaics" / "safe" / "valid_mask.tif").is_file()


def test_unknown_risk_file_without_wayback_reference_does_not_block_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    _write_file(runtime / "requests" / "large" / "run_response.json", b'{"note":"' + b"x" * 64 + b'"}')

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    assert any(item["cache_key"] == "safe" for item in report["cleanup_candidates"])
    scan = next(item for item in report["unknown_risk_items"] if item["path"].endswith("large/run_response.json"))
    assert scan["classification"] == "no_wayback_reference_found"
    assert scan["blocked_candidate_cache_keys"] == []


def test_unknown_risk_file_with_candidate_mosaic_tif_blocks_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    target = runtime / "wayback_mosaics" / "safe" / "mosaic.tif"
    _write_file(runtime / "requests" / "large" / "run_response.json", f'{{"path":"{target}"}}'.encode())

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    assert not any(item["cache_key"] == "safe" for item in report["cleanup_candidates"])
    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "safe")
    assert "referenced_by_unknown_risk_metadata" in item["protection_reasons"]
    assert report["candidate_blockers"][0]["cache_key"] == "safe"


def test_unknown_risk_file_with_candidate_mosaic_png_blocks_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    target = runtime / "wayback_mosaics" / "safe" / "mosaic.png"
    _write_file(runtime / "temporal_projects" / "large" / "project.json", f'{{"path":"{target}"}}'.encode())

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "safe")
    assert "referenced_by_unknown_risk_metadata" in item["protection_reasons"]


def test_unknown_risk_file_with_candidate_wayback_cache_key_blocks_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    _write_file(runtime / "requests" / "large" / "run_response.json", b'{"path":"wayback_mosaics/safe/mosaic.tif"}')

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "safe")
    assert "referenced_by_unknown_risk_metadata" in item["protection_reasons"]


def test_unknown_risk_file_with_unrelated_wayback_key_does_not_block_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    _write_file(runtime / "requests" / "large" / "run_response.json", b'{"path":"wayback_mosaics/recent/mosaic.tif"}')

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    assert any(item["cache_key"] == "safe" for item in report["cleanup_candidates"])
    scan = next(item for item in report["unknown_risk_items"] if item["path"].endswith("large/run_response.json"))
    assert scan["classification"] == "references_other_mosaic"
    assert scan["blocked_candidate_cache_keys"] == []


def test_large_unknown_metadata_is_scanned_in_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    monkeypatch.setattr(module, "UNKNOWN_RISK_SCAN_CHUNK_BYTES", 8)
    runtime = _runtime(tmp_path)
    split_reference = b"prefix wayback_mosaics/" + b"safe/mosaic.tif suffix"
    _write_file(runtime / "requests" / "large" / "run_response.json", split_reference)

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    scan = next(item for item in report["unknown_risk_items"] if item["path"].endswith("large/run_response.json"))
    assert scan["classification"] == "references_candidate_mosaic"
    assert scan["blocked_candidate_cache_keys"] == ["safe"]


def test_unknown_risk_scan_failure_keeps_candidate_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    _write_file(runtime / "requests" / "large" / "run_response.json", b"x" * 64)
    monkeypatch.setattr(
        module,
        "scan_unknown_risk_file",
        lambda path, needles: {
            "path": str(path),
            "size_bytes": 64,
            "classification": "scan_failed",
            "matched_cache_keys": [],
            "matched_paths": [],
            "blocked_candidate_cache_keys": [],
            "reason": "forced_failure",
        },
    )

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)

    item = next(item for item in report["protected_mosaics"] if item["cache_key"] == "safe")
    assert "unknown_risk_scan_failed" in item["protection_reasons"]


def test_unknown_risk_summary_counts_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    _write_file(runtime / "requests" / "large-a" / "run_response.json", b"no wayback here" * 8)
    _write_file(runtime / "requests" / "large-b" / "run_response.json", b"wayback_mosaics/safe/mosaic.tif")

    report = module.build_report(runtime, apply=False, yes=False, older_than_hours=72, max_rows=100)
    summary = report["unknown_risk_summary"]

    assert summary["total"] == 2
    assert summary["no_wayback_reference_found"] == 1
    assert summary["references_candidate_mosaic"] == 1


def test_apply_refuses_candidate_blocked_by_unknown_risk_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    _force_unknown_for_paths(module, monkeypatch)
    runtime = _runtime(tmp_path)
    _write_file(runtime / "requests" / "large" / "run_response.json", b"wayback_mosaics/safe/mosaic.tif")

    report = module.build_report(runtime, apply=True, yes=True, older_than_hours=72, max_rows=100)

    assert any(error["error"] == "candidate_blocked_by_unknown_risk" for error in report["errors"])
    assert (runtime / "wayback_mosaics" / "safe" / "mosaic.tif").is_file()
    assert (runtime / "wayback_mosaics" / "safe" / "mosaic.png").is_file()


def test_forbidden_cache_areas_are_never_touched(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _runtime(tmp_path)

    module.build_report(runtime, apply=True, yes=True, older_than_hours=72, max_rows=100)

    for folder in ("imagery_cache", "temporal_projects", "requests", "db_payloads", "wayback_tiles"):
        assert (runtime / folder / "sentinel" / "keep.bin").is_file()


def test_json_output_validates(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)

    json.loads(json.dumps(report))


def test_markdown_output_includes_protection_reasons(tmp_path: Path) -> None:
    module = _load_module()
    report = module.build_report(_runtime(tmp_path), apply=False, yes=False, older_than_hours=72, max_rows=100)
    markdown = module.render_markdown(report, 100)

    assert "## Protected Mosaics" in markdown
    assert "## Unknown-Risk Metadata Scan" in markdown
    assert "## Candidate-Specific Blockers" in markdown
    assert "## Unknown-Risk Items Not Blocking Candidates" in markdown
    assert "missing_canonical_cog" in markdown
    assert "referenced_by_request_metadata" in markdown
