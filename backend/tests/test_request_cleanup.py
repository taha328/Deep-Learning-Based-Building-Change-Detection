from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings
from src.services.request_cleanup import (
    audit_request_deletion_safety,
    cleanup_request_after_successful_promotion,
    run_post_completion_request_cleanup_if_enabled,
)


def _write_request(settings: Settings, request_hash: str = "req-1", *, success: bool = True) -> Path:
    request_dir = settings.request_cache_dir / request_hash
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / "run_response.json").write_text(json.dumps({"success": success, "summary": {"request_hash": request_hash}}), encoding="utf-8")
    (request_dir / "manifest.json").write_text(json.dumps({"success": success}), encoding="utf-8")
    (request_dir / "timing.json").write_text("{}", encoding="utf-8")
    (request_dir / "prediction_change_probability.tif").write_bytes(b"prob")
    (request_dir / "prediction_change_mask.tif").write_bytes(b"mask")
    (request_dir / "prediction_change_polygons.geojsonl").write_text("{}", encoding="utf-8")
    (request_dir / "export_bundle.zip").write_bytes(b"zip")
    tiles = request_dir / "tiles"
    tiles.mkdir()
    (tiles / "tile.bin").write_bytes(b"tile")
    return request_dir


def _write_project(settings: Settings, request_hash: str = "req-1", *, artifact_in_request: bool = False, reference_in_request: bool = False) -> Path:
    project_id = "temporal-demo"
    project_dir = settings.temporal_projects_dir / project_id
    milestone_dir = project_dir / "milestones" / "WB_2026_R04"
    milestone_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = settings.request_cache_dir / request_hash / "building_change_polygons.geojson" if artifact_in_request else milestone_dir / "additions.geojson"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    ref_path = settings.request_cache_dir / request_hash / "t2_wayback_rgb.tif" if reference_in_request else milestone_dir / "reference_imagery_cog.tif"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_bytes(b"tif")
    payload = {
        "project_id": project_id,
        "name": "Demo",
        "aoi_geojson": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "status": "complete",
                "pair_request_hash": request_hash,
                "reference_imagery": {"cog_path": str(ref_path), "tilejson_url": "/tilejson", "tiles_url_template": "/tiles/{z}/{x}/{y}.png"},
                "artifacts": [{"key": "additions", "path": str(artifact_path)}],
            }
        ],
    }
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    return project_dir


def test_heavy_compaction_dry_run_deletes_nothing(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request_dir = _write_request(settings)
    _write_project(settings)

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=True,
    )

    assert report.bytes_planned > 0
    assert (request_dir / "tiles").is_dir()
    assert (request_dir / "prediction_change_probability.tif").is_file()
    assert report.deleted == []


def test_heavy_compaction_apply_deletes_only_heavy_files_and_preserves_provenance(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_delete_export_bundle=False)
    request_dir = _write_request(settings)
    _write_project(settings)

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )

    assert report.bytes_deleted > 0
    assert not (request_dir / "tiles").exists()
    assert not (request_dir / "prediction_change_probability.tif").exists()
    assert (request_dir / "run_response.json").is_file()
    assert (request_dir / "manifest.json").is_file()
    assert (request_dir / "export_bundle.zip").is_file()
    assert (request_dir / "cleanup_report.json").is_file()


def test_cleanup_blocks_failed_and_incomplete_requests(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    _write_request(settings, "failed", success=False)
    _write_project(settings, "failed")
    incomplete = settings.request_cache_dir / "incomplete"
    incomplete.mkdir(parents=True)
    (incomplete / "prediction_change_mask.tif").write_bytes(b"mask")

    failed = audit_request_deletion_safety(request_hash="failed", settings=settings, project_id="temporal-demo")
    unknown = audit_request_deletion_safety(request_hash="incomplete", settings=settings)

    assert not failed.safe_to_compact_heavy
    assert "request_status_failed" in failed.blockers
    assert not unknown.safe_to_compact_heavy
    assert unknown.status in {"incomplete", "unknown"}


def test_cleanup_blocks_project_artifact_and_reference_paths_pointing_to_request(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    _write_request(settings)
    _write_project(settings, artifact_in_request=True)
    artifact_audit = audit_request_deletion_safety(request_hash="req-1", settings=settings, project_id="temporal-demo")
    assert not artifact_audit.safe_to_compact_heavy
    assert "project_artifact_path_points_to_request" in artifact_audit.blockers

    other_settings = Settings(runtime_cache_dir=tmp_path / "other_runtime")
    _write_request(other_settings)
    _write_project(other_settings, reference_in_request=True)
    reference_audit = audit_request_deletion_safety(request_hash="req-1", settings=other_settings, project_id="temporal-demo")
    assert not reference_audit.safe_to_compact_heavy
    assert "reference_imagery_path_points_to_request" in reference_audit.blockers


def test_full_deletion_is_refused_for_referenced_request_and_requires_settings(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request_dir = _write_request(settings)
    _write_project(settings)

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="delete_full",
        settings=settings,
        dry_run=False,
    )

    assert report.skipped
    assert report.reason == "audit_not_safe_to_delete_full"
    assert request_dir.exists()


def test_completion_hook_respects_disabled_setting(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_enabled=False)
    _write_request(settings)
    _write_project(settings)

    assert run_post_completion_request_cleanup_if_enabled(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        settings=settings,
    ) is None


def test_completion_hook_runs_compact_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        post_completion_request_cleanup_enabled=True,
        post_completion_request_cleanup_mode="compact_heavy",
    )
    request_dir = _write_request(settings)
    _write_project(settings)

    report = run_post_completion_request_cleanup_if_enabled(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        settings=settings,
    )

    assert report is not None
    assert not (request_dir / "tiles").exists()


def test_default_post_completion_cleanup_settings_match_operator_policy(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")

    assert settings.post_completion_request_cleanup_enabled is True
    assert settings.post_completion_request_cleanup_mode == "compact_heavy"
    assert settings.post_completion_request_cleanup_grace_seconds == 300
    assert settings.post_completion_request_cleanup_keep_provenance is True
    assert settings.post_completion_request_cleanup_delete_export_bundle is True


def test_export_bundle_deleted_by_default_and_can_be_kept_when_configured(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request_dir = _write_request(settings)
    _write_project(settings)
    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )
    assert not (request_dir / "export_bundle.zip").exists()

    keep_bundle = Settings(
        runtime_cache_dir=tmp_path / "runtime_keep_bundle",
        post_completion_request_cleanup_delete_export_bundle=False,
    )
    keep_bundle_request = _write_request(keep_bundle)
    _write_project(keep_bundle)
    keep_bundle_report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=keep_bundle,
        dry_run=False,
    )
    assert report.bytes_deleted >= keep_bundle_report.bytes_deleted
    assert (keep_bundle_request / "export_bundle.zip").is_file()
