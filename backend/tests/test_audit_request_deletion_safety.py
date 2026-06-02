from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings

from scripts.audit_request_deletion_safety import build_report


def _seed(runtime: Path) -> Settings:
    settings = Settings(runtime_cache_dir=runtime)
    request_dir = settings.request_cache_dir / "req-1"
    request_dir.mkdir(parents=True)
    (request_dir / "run_response.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    (request_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (request_dir / "prediction_change_mask.tif").write_bytes(b"mask")
    (request_dir / "tiles").mkdir()
    (request_dir / "tiles" / "tile").write_bytes(b"tile")
    project_dir = settings.temporal_projects_dir / "temporal-demo"
    artifact = project_dir / "milestones" / "WB_2026_R04" / "additions.geojson"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    payload = {
        "project_id": "temporal-demo",
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "pair_request_hash": "req-1",
                "artifacts": [{"key": "additions", "path": str(artifact)}],
            }
        ],
    }
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    return settings


def test_auditor_reports_single_request_by_hash(tmp_path: Path) -> None:
    _seed(tmp_path / "runtime")

    report = build_report(runtime_cache_dir=tmp_path / "runtime", request_hash="req-1")

    assert report["request_count"] == 1
    item = report["reports"][0]
    assert item["request_hash"] == "req-1"
    assert item["status"] == "completed"
    assert item["safe_to_compact_heavy"] is True
    assert item["recommended_policy"] == "compact_heavy"


def test_auditor_can_select_project_hashes(tmp_path: Path) -> None:
    _seed(tmp_path / "runtime")

    report = build_report(runtime_cache_dir=tmp_path / "runtime", project_id="temporal-demo")

    assert report["request_count"] == 1
    assert report["reports"][0]["project_ids"] == ["temporal-demo"]


def test_auditor_json_shape_contains_required_keys(tmp_path: Path) -> None:
    _seed(tmp_path / "runtime")

    item = build_report(runtime_cache_dir=tmp_path / "runtime", request_hash="req-1")["reports"][0]

    for key in {
        "request_hash",
        "project_ids",
        "status",
        "promotion_status",
        "frontend_dependencies",
        "qgis_dependencies",
        "backend_dependencies",
        "export_dependencies",
        "repair_dependencies",
        "artifact_paths_pointing_to_request",
        "reference_imagery_paths_pointing_to_request",
        "heavy_files",
        "provenance_files",
        "safe_to_delete_full",
        "safe_to_compact_heavy",
        "recommended_policy",
        "blockers",
    }:
        assert key in item
