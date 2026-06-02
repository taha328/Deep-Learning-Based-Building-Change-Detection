from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings
from scripts.verify_post_completion_request_cleanup import verify_cleanup


def _seed(runtime: Path) -> Settings:
    settings = Settings(runtime_cache_dir=runtime)
    request_dir = settings.request_cache_dir / "req-1"
    request_dir.mkdir(parents=True)
    (request_dir / "run_response.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    (request_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (request_dir / "prediction_change_mask.tif").write_bytes(b"mask")
    (request_dir / "prediction_change_probability.tif").write_bytes(b"prob")
    (request_dir / "tiles").mkdir()
    (request_dir / "tiles" / "tile").write_bytes(b"tile")
    project_dir = settings.temporal_projects_dir / "temporal-demo"
    milestone_dir = project_dir / "milestones" / "WB_2026_R04"
    milestone_dir.mkdir(parents=True)
    additions = milestone_dir / "additions.geojson"
    additions.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    ref = milestone_dir / "reference_imagery_cog.tif"
    ref.write_bytes(b"tif")
    payload = {
        "project_id": "temporal-demo",
        "download_bundle_path": None,
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "pair_request_hash": "req-1",
                "reference_imagery": {"cog_path": str(ref), "tilejson_url": "/tilejson", "tiles_url_template": "/tiles/{z}/{x}/{y}.png"},
                "artifacts": [{"key": "additions", "path": str(additions)}],
            }
        ],
    }
    (project_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    return settings


def test_verify_cleanup_dry_run_preserves_request(tmp_path: Path) -> None:
    settings = _seed(tmp_path / "runtime")

    report = verify_cleanup(
        runtime_cache_dir=settings.runtime_cache_dir,
        project_id="temporal-demo",
        request_hash="req-1",
        mode="compact_heavy",
        apply=False,
        yes=False,
    )

    assert report["cleanup"]["dry_run"] is True
    assert (settings.request_cache_dir / "req-1" / "tiles").exists()
    assert report["after_checks"]["project_api_still_loads"] is True
    assert report["after_checks"]["reference_imagery_present"] is True


def test_verify_cleanup_apply_keeps_project_usable(tmp_path: Path) -> None:
    settings = _seed(tmp_path / "runtime")

    report = verify_cleanup(
        runtime_cache_dir=settings.runtime_cache_dir,
        project_id="temporal-demo",
        request_hash="req-1",
        mode="compact_heavy",
        apply=True,
        yes=True,
    )

    assert report["cleanup"]["dry_run"] is False
    assert not (settings.request_cache_dir / "req-1" / "tiles").exists()
    assert report["after_checks"]["project_api_still_loads"] is True
    assert report["after_checks"]["reference_imagery_present"] is True
    assert report["after_checks"]["temporal_vector_artifacts_exist"] is True
    assert report["after_checks"]["metadata_paths_pointing_to_request"] == []


def test_verify_cleanup_apply_requires_yes(tmp_path: Path) -> None:
    settings = _seed(tmp_path / "runtime")

    report = verify_cleanup(
        runtime_cache_dir=settings.runtime_cache_dir,
        project_id="temporal-demo",
        request_hash="req-1",
        mode="compact_heavy",
        apply=True,
        yes=False,
    )

    assert report["error"] == "apply_requires_yes"
