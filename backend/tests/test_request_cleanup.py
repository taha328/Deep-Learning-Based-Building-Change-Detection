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
    (request_dir / "change_probability.tif").write_bytes(b"prob2")
    (request_dir / "building_change_mask.tif").write_bytes(b"mask2")
    (request_dir / "building_change_labels.tif").write_bytes(b"labels")
    (request_dir / "building_change_polygons.geojsonl").write_text("{}", encoding="utf-8")
    (request_dir / "export_bundle.zip").write_bytes(b"zip")
    tiles = request_dir / "tiles"
    tiles.mkdir()
    (tiles / "tile.bin").write_bytes(b"tile")
    return request_dir


def _write_project(
    settings: Settings,
    request_hash: str = "req-1",
    *,
    pair_request_hash: str | None = None,
    artifact_in_request: bool = False,
    reference_in_request: bool = False,
) -> Path:
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
                "pair_request_hash": pair_request_hash or request_hash,
                "populated_request_hash": request_hash,
                "request_workspace_path": str(settings.request_cache_dir / request_hash),
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
    assert not (request_dir / "change_probability.tif").exists()
    assert not (request_dir / "building_change_mask.tif").exists()
    assert (request_dir / "run_response.json").is_file()
    assert (request_dir / "manifest.json").is_file()
    assert (request_dir / "export_bundle.zip").is_file()
    assert (request_dir / "cleanup_report.json").is_file()
    assert (request_dir / "cleanup_audit.json").is_file()


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


def test_completion_hook_targets_populated_request_hash_when_pair_hash_differs(tmp_path: Path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        post_completion_request_cleanup_enabled=True,
        post_completion_request_cleanup_mode="compact_heavy",
    )
    pair_dir = _write_request(settings, "logical-pair")
    populated_dir = _write_request(settings, "physical-run")
    _write_project(settings, "physical-run", pair_request_hash="logical-pair")

    report = run_post_completion_request_cleanup_if_enabled(
        request_hash="logical-pair",
        pair_request_hash="logical-pair",
        populated_request_hash="physical-run",
        request_workspace_path=str(populated_dir),
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        settings=settings,
    )

    assert report is not None
    assert report.request_hash == "physical-run"
    assert report.pair_request_hash == "logical-pair"
    assert report.populated_request_hash == "physical-run"
    assert not report.skipped
    assert (pair_dir / "tiles").is_dir()
    assert not (populated_dir / "tiles").exists()
    assert not (populated_dir / "change_probability.tif").exists()
    audit = json.loads((populated_dir / "cleanup_audit.json").read_text(encoding="utf-8"))
    assert audit["pair_request_hash"] == "logical-pair"
    assert audit["populated_request_hash"] == "physical-run"


def test_noop_compaction_preserves_prior_successful_cleanup_audit(tmp_path: Path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        post_completion_request_cleanup_enabled=True,
        post_completion_request_cleanup_mode="compact_heavy",
    )
    request_dir = _write_request(settings)
    _write_project(settings)

    first = run_post_completion_request_cleanup_if_enabled(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        settings=settings,
    )
    assert first is not None and not first.skipped
    audit_path = request_dir / "cleanup_audit.json"
    first_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert first_audit["skipped"] is False

    second = run_post_completion_request_cleanup_if_enabled(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        settings=settings,
    )
    assert second is not None and second.skipped
    assert json.loads(audit_path.read_text(encoding="utf-8"))["skipped"] is False


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


def test_compaction_deletes_request_local_published_geojson_and_csv_duplicates(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_delete_export_bundle=False)
    request_dir = _write_request(settings)
    _write_project(settings)
    (request_dir / "building_change_polygons.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    (request_dir / "building_change_polygons.csv").write_text("id\n1\n", encoding="utf-8")

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )

    assert not report.skipped
    assert not (request_dir / "building_change_polygons.geojson").exists()
    assert not (request_dir / "building_change_polygons.csv").exists()
    deleted_names = {Path(item["path"]).name for item in report.deleted_published_duplicates}
    assert {"building_change_polygons.geojson", "building_change_polygons.csv"} <= deleted_names
    audit = json.loads((request_dir / "cleanup_audit.json").read_text(encoding="utf-8"))
    audit_names = {Path(item["path"]).name for item in audit["audit"]["published_duplicate_files"]}
    assert {"building_change_polygons.geojson", "building_change_polygons.csv"} <= audit_names


def test_compaction_preserves_request_artifact_when_project_copy_is_missing(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_delete_export_bundle=False)
    request_dir = _write_request(settings)
    _write_project(settings)
    (request_dir / "addition_candidate_diagnostics.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )

    assert (request_dir / "addition_candidate_diagnostics.geojson").is_file()
    skipped_names = {Path(item["path"]).name for item in report.skipped_published_duplicate_candidates}
    assert "addition_candidate_diagnostics.geojson" in skipped_names


def test_compaction_deletes_request_t1_t2_when_project_reference_cogs_are_proven(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_delete_export_bundle=False)
    request_dir = _write_request(settings)
    project_dir = _write_project(settings)
    baseline_dir = project_dir / "milestones" / "WB_2020_R04"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / "reference_imagery_cog.tif").write_bytes(b"baseline-cog")
    target_dir = project_dir / "milestones" / "WB_2026_R04"
    (request_dir / "t1_wayback_rgb.tif").write_bytes(b"t1")
    (request_dir / "t2_wayback_rgb.tif").write_bytes(b"t2")
    project_json = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    project_json["milestones"].insert(
        0,
        {
            "release_identifier": "WB_2020_R04",
            "status": "complete",
            "reference_imagery": {"cog_path": str(baseline_dir / "reference_imagery_cog.tif")},
            "artifacts": [],
        },
    )
    (project_dir / "project.json").write_text(json.dumps(project_json), encoding="utf-8")
    (request_dir / "manifest.json").write_text(
        json.dumps(
            {
                "success": True,
                "imagery_sources": {
                    "t1": {
                        "release_identifier": "WB_2020_R04",
                        "canonical_cog_path": str(settings.reference_imagery_cache_dir / "baseline" / "reference_imagery_cog.tif"),
                        "project_cog_path": str(baseline_dir / "reference_imagery_cog.tif"),
                    },
                    "t2": {
                        "release_identifier": "WB_2026_R04",
                        "canonical_cog_path": str(settings.reference_imagery_cache_dir / "target" / "reference_imagery_cog.tif"),
                        "project_cog_path": str(target_dir / "reference_imagery_cog.tif"),
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )

    assert not (request_dir / "t1_wayback_rgb.tif").exists()
    assert not (request_dir / "t2_wayback_rgb.tif").exists()
    deleted_names = {Path(item["path"]).name for item in report.deleted_published_duplicates}
    assert {"t1_wayback_rgb.tif", "t2_wayback_rgb.tif"} <= deleted_names


def test_compaction_preserves_request_t1_t2_when_reference_proof_is_missing(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_delete_export_bundle=False)
    request_dir = _write_request(settings)
    _write_project(settings)
    (request_dir / "t1_wayback_rgb.tif").write_bytes(b"t1")
    (request_dir / "t2_wayback_rgb.tif").write_bytes(b"t2")

    report = cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )

    assert (request_dir / "t1_wayback_rgb.tif").is_file()
    assert (request_dir / "t2_wayback_rgb.tif").is_file()
    skipped_names = {Path(item["path"]).name for item in report.skipped_published_duplicate_candidates}
    assert {"t1_wayback_rgb.tif", "t2_wayback_rgb.tif"} <= skipped_names


def test_completed_project_payload_still_points_to_project_artifacts_after_request_duplicate_deletion(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", post_completion_request_cleanup_delete_export_bundle=False)
    request_dir = _write_request(settings)
    project_dir = _write_project(settings)
    (request_dir / "building_change_polygons.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    cleanup_request_after_successful_promotion(
        request_hash="req-1",
        project_id="temporal-demo",
        release_identifier="WB_2026_R04",
        mode="compact_heavy",
        settings=settings,
        dry_run=False,
    )

    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    artifact_path = Path(project["milestones"][0]["artifacts"][0]["path"])
    assert artifact_path.is_file()
    assert artifact_path.name == "additions.geojson"
    assert not str(artifact_path).startswith(str(request_dir))
