from __future__ import annotations

import json
import logging
from pathlib import Path

from src.config import Settings
from src.schemas import TemporalProject
from src.services.wayback_mosaic_cleanup import (
    cleanup_finalized_temporal_project_wayback_mosaics,
    cleanup_wayback_mosaic_cache_after_success,
)
from src.services.temporal_projects import _ensure_temporal_project_reference_imagery_from_canonical_cache


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _cleanup(tmp_path: Path, *, target: Path | None = None, final_exists: bool = True):
    runtime = tmp_path / "runtime_cache"
    mosaic_dir = target or runtime / "wayback_mosaics" / "cache-key"
    _write(mosaic_dir / "mosaic.tif", b"tif")
    _write(mosaic_dir / "mosaic.png", b"png")
    _write(mosaic_dir / "metadata.json", b"{}")
    _write(mosaic_dir / "valid_mask.tif", b"mask")
    final = runtime / "temporal_projects" / "project" / "milestones" / "release" / "reference_imagery_cog.tif"
    if final_exists:
        _write(final, b"final")
    result = cleanup_wayback_mosaic_cache_after_success(
        project_id="project",
        release_identifier="release",
        wayback_mosaic_dir=mosaic_dir,
        final_reference_path=final,
        wayback_mosaics_root=runtime / "wayback_mosaics",
        runtime_cache_root=runtime,
    )
    return runtime, mosaic_dir, result


def test_successful_cleanup_deletes_only_disposable_mosaic_files(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO)
    runtime, mosaic_dir, result = _cleanup(tmp_path)

    assert result == {"cleaned": True, "reason": None, "bytes_freed": 6}
    assert not (mosaic_dir / "mosaic.tif").exists()
    assert not (mosaic_dir / "mosaic.png").exists()
    assert (mosaic_dir / "metadata.json").is_file()
    assert (mosaic_dir / "valid_mask.tif").is_file()
    assert "WAYBACK_MOSAIC_CACHE_CLEANED" in caplog.text
    assert "bytesFreed=6" in caplog.text
    assert not any((runtime / name).exists() for name in ("imagery_cache", "reference_tiles"))


def test_cleanup_skips_when_final_reference_is_missing(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING)
    _runtime, mosaic_dir, result = _cleanup(tmp_path, final_exists=False)

    assert result["reason"] == "FINAL_REFERENCE_MISSING"
    assert (mosaic_dir / "mosaic.tif").is_file()
    assert "reason=FINAL_REFERENCE_MISSING" in caplog.text


def test_cleanup_refuses_outside_path_and_wayback_root(tmp_path: Path) -> None:
    runtime, outside, outside_result = _cleanup(tmp_path, target=tmp_path / "outside")
    root_result = cleanup_wayback_mosaic_cache_after_success(
        project_id="project",
        release_identifier="release",
        wayback_mosaic_dir=runtime / "wayback_mosaics",
        final_reference_path=runtime / "temporal_projects" / "project" / "milestones" / "release" / "reference_imagery_cog.tif",
        wayback_mosaics_root=runtime / "wayback_mosaics",
        runtime_cache_root=runtime,
    )

    assert outside_result["reason"] == "UNSAFE_PATH"
    assert (outside / "mosaic.tif").is_file()
    assert root_result["reason"] == "UNSAFE_PATH"


def test_cleanup_preserves_imagery_cache_and_temporal_project_outputs(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    imagery = _write(runtime / "imagery_cache" / "entry" / "reference_imagery_cog.tif", b"canonical")
    project_output = _write(runtime / "temporal_projects" / "project" / "milestones" / "release" / "reference_imagery_cog.tif", b"final")
    mosaic_dir = runtime / "wayback_mosaics" / "cache-key"
    _write(mosaic_dir / "mosaic.tif")
    _write(mosaic_dir / "mosaic.png")

    cleanup_wayback_mosaic_cache_after_success(
        project_id="project",
        release_identifier="release",
        wayback_mosaic_dir=mosaic_dir,
        final_reference_path=project_output,
        wayback_mosaics_root=runtime / "wayback_mosaics",
        runtime_cache_root=runtime,
    )

    assert imagery.is_file()
    assert project_output.is_file()


def test_cleanup_skips_active_project_reference(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    reference = runtime / "wayback_mosaics" / "cache-key" / "mosaic.tif"
    project_json = runtime / "temporal_projects" / "other-project" / "project.json"
    _write(project_json, json.dumps({"image_path": str(reference)}).encode())
    _runtime, mosaic_dir, result = _cleanup(tmp_path)

    assert result["reason"] == "ACTIVE_REFERENCE"
    assert (mosaic_dir / "mosaic.tif").is_file()


def test_cleanup_skips_active_request_reference(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    reference = runtime / "wayback_mosaics" / "cache-key" / "mosaic.png"
    _write(runtime / "requests" / "other-run" / "run_response.json", json.dumps({"image_path": str(reference)}).encode())
    _runtime, mosaic_dir, result = _cleanup(tmp_path)

    assert result["reason"] == "ACTIVE_REFERENCE"
    assert (mosaic_dir / "mosaic.png").is_file()


def test_project_cleanup_runs_only_after_all_milestones_complete(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache")
    mosaic_dir = settings.wayback_mosaic_cache_dir / "cache-key"
    _write(mosaic_dir / "mosaic.tif")
    _write(mosaic_dir / "mosaic.png")
    canonical = _write(settings.reference_imagery_cache_dir / "canonical" / "reference_imagery_cog.tif")
    _write(canonical.with_name("metadata.json"), json.dumps({"source_wayback_mosaic_dir": str(mosaic_dir)}).encode())
    final = _write(settings.temporal_projects_dir / "project" / "milestones" / "release" / "reference_imagery_cog.tif")
    project = TemporalProject(
        project_id="project",
        name="Project",
        project_dir=str(settings.temporal_projects_dir / "project"),
        milestones=[
            {
                "release_identifier": "release",
                "status": "error",
                "reference_imagery": {
                    "cog_path": str(final),
                    "canonical_cog_path": str(canonical),
                },
            }
        ],
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    )

    assert cleanup_finalized_temporal_project_wayback_mosaics(project=project, settings=settings) == []
    assert (mosaic_dir / "mosaic.tif").is_file()
    project.milestones[0].status = "complete"
    cleanup_finalized_temporal_project_wayback_mosaics(project=project, settings=settings)
    assert not (mosaic_dir / "mosaic.tif").exists()
    assert final.is_file()
    reloaded = TemporalProject.model_validate_json(project.model_dump_json())
    assert reloaded.milestones[0].reference_imagery.cog_path == str(final)


def test_project_cleanup_respects_disabled_post_completion_cleanup(tmp_path: Path, caplog) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime_cache",
        post_completion_request_cleanup_enabled=False,
    )
    project = TemporalProject(
        project_id="project",
        name="Project",
        milestones=[{"release_identifier": "release", "status": "complete"}],
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    )
    caplog.set_level(logging.INFO)

    assert cleanup_finalized_temporal_project_wayback_mosaics(project=project, settings=settings) == []
    assert "reason=CONFIG_DISABLED" in caplog.text


def test_existing_project_cog_is_linked_back_to_canonical_metadata(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache")
    project_dir = settings.temporal_projects_dir / "project"
    final = _write(project_dir / "milestones" / "release" / "reference_imagery_cog.tif", b"final")
    canonical = _write(settings.reference_imagery_cache_dir / "canonical" / "reference_imagery_cog.tif", b"canonical")
    canonical_metadata = {
        "reference_imagery_key": "refimg-v1-test",
        "canonical_cog_path": str(canonical),
        "materializations": [],
    }
    project = TemporalProject(
        project_id="project",
        name="Project",
        project_dir=str(project_dir),
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        },
        milestones=[
            {
                "release_identifier": "release",
                "status": "complete",
                "reference_imagery": {"cog_path": str(final)},
            }
        ],
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    )
    monkeypatch.setattr(
        "src.services.temporal_projects._find_matching_canonical_reference_imagery",
        lambda **_kwargs: (canonical, canonical_metadata),
    )

    count = _ensure_temporal_project_reference_imagery_from_canonical_cache(
        project=project,
        settings=settings,
        project_dir=project_dir,
    )

    reference = project.milestones[0].reference_imagery
    assert count == 1
    assert reference is not None
    assert reference.reference_imagery_key == "refimg-v1-test"
    assert reference.canonical_cog_path == str(canonical)
    assert final.read_bytes() == b"final"
