from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.domain.reference_imagery_cache import (
    build_aoi_hash,
    write_reference_imagery_cache_metadata,
)

import scripts.repair_temporal_reference_imagery_links as repair_script


def _aoi(offset: float = 0.0) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [-7.0 + offset, 33.0],
                [-6.99 + offset, 33.0],
                [-6.99 + offset, 33.01],
                [-7.0 + offset, 33.01],
                [-7.0 + offset, 33.0],
            ]
        ],
    }


def _write_cog(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.zeros((3, 16, 16), dtype=np.uint8)
    data[0] = 100
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=16,
        height=16,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(-1000.0, 1000.0, 10.0, 10.0),
    ) as dst:
        dst.write(data)


def _write_project(runtime: Path, *, project_id: str = "temporal-demo", release: str = "WB_2026_R04") -> Path:
    project_dir = runtime / "temporal_projects" / project_id
    payload = {
        "project_id": project_id,
        "name": "Demo",
        "project_dir": str(project_dir),
        "aoi_geojson": _aoi(),
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "milestones": [
            {
                "release_identifier": release,
                "release_date": "2026-04-30",
                "pair_request_hash": "keep-me",
                "artifacts": [{"key": "additions", "path": "keep.geojson"}],
            }
        ],
    }
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    (project_dir / "project_manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return project_dir


def _write_canonical(runtime: Path, *, release: str = "WB_2026_R04", key: str = "refimg-v1-demo", aoi_offset: float = 0.0) -> Path:
    entry_dir = runtime / "imagery_cache" / key
    cog = entry_dir / "reference_imagery_cog.tif"
    _write_cog(cog)
    metadata = {
        "reference_imagery_key": key,
        "canonical_cog_path": str(cog),
        "release_identifier": release,
        "aoi_hash": build_aoi_hash(_aoi(aoi_offset)),
        "materializations": [],
    }
    write_reference_imagery_cache_metadata(entry_dir / "metadata.json", metadata)
    return cog


def test_repair_dry_run_reports_candidate_without_writing(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    _write_project(runtime)
    _write_canonical(runtime)

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo")

    project_cog = runtime / "temporal_projects" / "temporal-demo" / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    assert report["mode"] == "dry-run"
    assert report["summary"]["repair_candidates"] == 1
    assert not project_cog.exists()


def test_apply_requires_yes(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    _write_project(runtime)
    _write_canonical(runtime)

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo", apply=True, yes=False)

    assert report["mode"] == "apply-refused"
    assert report["errors"][0]["reason"] == "apply_requires_yes"


def test_repair_apply_materializes_link_and_updates_metadata(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    project_dir = _write_project(runtime)
    canonical = _write_canonical(runtime)

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo", apply=True, yes=True)

    project_cog = project_dir / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    project = json.loads((project_dir / "project.json").read_text())
    manifest = json.loads((project_dir / "project_manifest.json").read_text())
    reference = project["milestones"][0]["reference_imagery"]
    canonical_metadata = json.loads((canonical.parent / "metadata.json").read_text())

    assert report["summary"]["links_created"] == 1
    assert project_cog.exists()
    assert project_cog.read_bytes() == canonical.read_bytes()
    assert reference["reference_imagery_key"] == "refimg-v1-demo"
    assert reference["canonical_cog_path"] == str(canonical)
    assert reference["cog_path"] == str(project_cog)
    assert reference["tilejson_url"].endswith("/reference/tilejson.json")
    assert reference["tiles_url_template"].endswith("/reference/tiles/{z}/{x}/{y}.png")
    assert project["milestones"][0]["pair_request_hash"] == "keep-me"
    assert project["milestones"][0]["artifacts"] == [{"key": "additions", "path": "keep.geojson"}]
    assert manifest["milestones"][0]["reference_imagery"]["cog_path"] == str(project_cog)
    assert canonical_metadata["materializations"][0]["project_id"] == "temporal-demo"


def test_existing_valid_reference_is_not_repaired(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    project_dir = _write_project(runtime)
    _write_canonical(runtime)
    project_cog = project_dir / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    _write_cog(project_cog)
    payload = json.loads((project_dir / "project.json").read_text())
    payload["milestones"][0]["reference_imagery"] = {
        "cog_path": str(project_cog),
        "tilejson_url": "/tilejson",
        "tiles_url_template": "/tiles/{z}/{x}/{y}.png",
    }
    (project_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo", apply=True, yes=True)

    assert report["summary"]["already_valid"] == 1
    assert report["summary"]["links_created"] == 0


def test_ambiguous_canonical_match_is_protected(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    _write_project(runtime)
    _write_canonical(runtime, key="refimg-v1-a")
    _write_canonical(runtime, key="refimg-v1-b")

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo", apply=True, yes=True)

    assert report["summary"]["protected_milestones"] == 1
    assert report["protected_milestones"][0]["reason"] == "protected_ambiguous_canonical_match"
    project_cog = runtime / "temporal_projects" / "temporal-demo" / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    assert not project_cog.exists()


def test_wrong_aoi_canonical_match_is_protected(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    _write_project(runtime)
    _write_canonical(runtime, aoi_offset=1.0)

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo")

    assert report["summary"]["protected_milestones"] == 1
    assert report["protected_milestones"][0]["reason"] == "protected_no_canonical_cog"


def test_symlink_and_copy_fallback_methods_are_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    project_dir = _write_project(runtime)
    _write_canonical(runtime)

    def fake_materialize(*, canonical_cog_path: Path, project_cog_path: Path, mode: str = "hardlink") -> dict[str, object]:
        project_cog_path.parent.mkdir(parents=True, exist_ok=True)
        project_cog_path.symlink_to(canonical_cog_path)
        return {"method": "symlink", "project_cog_path": str(project_cog_path), "canonical_cog_path": str(canonical_cog_path)}

    monkeypatch.setattr(repair_script, "materialize_reference_imagery_cog", fake_materialize)

    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo", apply=True, yes=True)
    assert report["links_created"][0]["method"] == "symlink"
    assert (project_dir / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif").is_symlink()

    other_runtime = tmp_path / "runtime_cache_copy"
    _write_project(other_runtime)
    _write_canonical(other_runtime)

    def fake_copy(*, canonical_cog_path: Path, project_cog_path: Path, mode: str = "hardlink") -> dict[str, object]:
        project_cog_path.parent.mkdir(parents=True, exist_ok=True)
        project_cog_path.write_bytes(canonical_cog_path.read_bytes())
        return {"method": "copy", "project_cog_path": str(project_cog_path), "canonical_cog_path": str(canonical_cog_path)}

    monkeypatch.setattr(repair_script, "materialize_reference_imagery_cog", fake_copy)
    copy_report = repair_script.build_report(runtime_cache_dir=other_runtime, project_id="temporal-demo", apply=True, yes=True)
    assert copy_report["links_created"][0]["method"] == "copy"


def test_markdown_report_has_required_sections(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    _write_project(runtime)
    _write_canonical(runtime)
    report = repair_script.build_report(runtime_cache_dir=runtime, project_id="temporal-demo")

    markdown = repair_script._markdown_report(report)

    assert "## Summary" in markdown
    assert "## repair_candidates" in markdown
    assert "## protected_milestones" in markdown
