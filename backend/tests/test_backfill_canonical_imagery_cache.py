from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import rasterio
from rasterio.transform import from_bounds


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill_canonical_imagery_cache.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_canonical_imagery_cache", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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


def _write_wayback(runtime: Path, cache_key: str = "cache-key", *, valid: bool = True, metadata: dict | None = None) -> Path:
    folder = runtime / "wayback_mosaics" / cache_key
    if valid:
        _write_rgb(folder / "mosaic.tif")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mosaic.tif").write_bytes(b"not-a-tif")
    (folder / "mosaic.png").write_bytes(b"png")
    _write_mask(folder / "valid_mask.tif")
    payload = {
        "release_identifier": "WB_2026_R04",
        "release_num": 4,
        "tile_matrix_set": "default028mm",
        "zoom": 18,
        "tile_range": [0, 0, 0, 0],
        "bounds_3857": [0.0, 0.0, 256.0, 256.0],
    }
    if metadata:
        payload.update(metadata)
    _write_json(folder / "metadata.json", payload)
    return folder


def _write_project(runtime: Path, project_id: str = "temporal-demo") -> Path:
    cog = runtime / "temporal_projects" / project_id / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    _write_rgb(cog)
    _write_json(
        runtime / "temporal_projects" / project_id / "project.json",
        {
            "project_id": project_id,
            "aoi_geojson": {"type": "Polygon", "coordinates": [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]]},
            "milestones": [
                {
                    "release_identifier": "WB_2026_R04",
                    "reference_imagery": {
                        "cog_path": str(cog),
                        "maxzoom": 18,
                        "tile_range": [0, 0, 0, 0],
                        "bounds_3857": [0.0, 0.0, 256.0, 256.0],
                    },
                }
            ],
        },
    )
    return cog


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime)

    report = module.build_report(runtime, apply=False, yes=False, project_id=None, max_rows=100)

    assert report["mode"] == "dry_run"
    assert report["summary"]["backfill_candidate_count"] == 1
    assert not any((runtime / "imagery_cache").glob("*/reference_imagery_cog.tif"))


def test_apply_without_yes_writes_nothing_and_reports_error(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime)

    report = module.build_report(runtime, apply=True, yes=False, project_id=None, max_rows=100)

    assert report["errors"][0]["error"] == "apply_requires_yes"
    assert not any((runtime / "imagery_cache").glob("*/reference_imagery_cog.tif"))


def test_wayback_mosaic_can_be_converted_and_linked(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime, "safe-wayback")

    report = module.build_report(runtime, apply=True, yes=True, project_id=None, max_rows=100)

    assert report["summary"]["canonical_cogs_created_count"] == 1
    created = report["canonical_cogs_created"][0]
    metadata = json.loads(Path(created["details"]["metadata_path"]).read_text())
    assert metadata["source_wayback_cache_key"] == "safe-wayback"
    assert metadata["source_wayback_mosaic_cache_key"] == "safe-wayback"
    assert Path(metadata["canonical_cog_path"]).is_file()
    assert Path(metadata["canonical_valid_mask_path"]).is_file()
    assert (runtime / "wayback_mosaics" / "safe-wayback" / "mosaic.tif").is_file()


def test_existing_project_cog_can_be_registered(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_project(runtime)

    report = module.build_report(runtime, apply=True, yes=True, project_id="temporal-demo", max_rows=100)

    assert report["summary"]["canonical_cogs_created_count"] == 1
    metadata = json.loads(Path(report["canonical_cogs_created"][0]["details"]["metadata_path"]).read_text())
    assert metadata["source_type"] == "temporal_project_cog"
    assert metadata["source_project_id"] == "temporal-demo"
    assert (runtime / "temporal_projects" / "temporal-demo" / "project.json").is_file()


def test_invalid_wayback_mosaic_is_reported_as_error(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime, "bad", valid=False)

    report = module.build_report(runtime, apply=True, yes=True, project_id=None, max_rows=100)

    assert report["summary"]["error_count"] == 1
    assert report["errors"][0]["status"] == "error"
    assert (runtime / "wayback_mosaics" / "bad" / "mosaic.tif").is_file()


def test_insufficient_metadata_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime, "missing-meta", metadata={"tile_range": None})

    report = module.build_report(runtime, apply=False, yes=False, project_id=None, max_rows=100)

    assert report["protected_sources"][0]["status"] == "protected_insufficient_metadata"
    assert "missing_tile_range" in report["protected_sources"][0]["reason"]


def test_existing_canonical_cog_is_reused_and_metadata_link_updated(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime, "reuse")

    first = module.build_report(runtime, apply=True, yes=True, project_id=None, max_rows=100)
    assert first["summary"]["canonical_cogs_created_count"] == 1
    second = module.build_report(runtime, apply=True, yes=True, project_id=None, max_rows=100)

    assert second["summary"]["already_backfilled_count"] == 1
    assert second["summary"]["metadata_links_written_count"] == 1


def test_json_output_shape_and_markdown_sections(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime)
    report = module.build_report(runtime, apply=False, yes=False, project_id=None, max_rows=100)
    json.dumps(report)
    markdown = module.render_markdown(report, 100)

    for section in (
        "# Canonical Imagery Cache Backfill Report",
        "## Mode",
        "## Runtime Cache Location",
        "## Summary",
        "## Sources Inspected",
        "## Backfill Candidates",
        "## Already Backfilled",
        "## Canonical COGs Created",
        "## Metadata Links Written",
        "## Protected Sources",
        "## Errors",
        "## Next Steps",
    ):
        assert section in markdown


def test_no_source_or_forbidden_files_are_deleted(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _write_wayback(runtime, "safe")
    for folder in ("requests", "temporal_projects", "db_payloads"):
        (runtime / folder / "sentinel.txt").parent.mkdir(parents=True, exist_ok=True)
        (runtime / folder / "sentinel.txt").write_text("keep")

    module.build_report(runtime, apply=True, yes=True, project_id=None, max_rows=100)

    assert (runtime / "wayback_mosaics" / "safe" / "mosaic.tif").is_file()
    assert (runtime / "wayback_mosaics" / "safe" / "mosaic.png").is_file()
    assert (runtime / "wayback_mosaics" / "safe" / "metadata.json").is_file()
    assert (runtime / "wayback_mosaics" / "safe" / "valid_mask.tif").is_file()
    for folder in ("requests", "temporal_projects", "db_payloads"):
        assert (runtime / folder / "sentinel.txt").read_text() == "keep"
