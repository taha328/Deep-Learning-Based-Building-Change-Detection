from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


COMPACT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compact_temporal_project_metadata.py"
INSPECT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_runtime_storage.py"
CLEANUP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_runtime_storage.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_compact_module():
    return _load_module(COMPACT_SCRIPT, "compact_temporal_project_metadata")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_file(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _feature_collection(count: int) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"feature_index": index},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-7.0 - index * 0.001, 33.0],
                            [-7.0 - index * 0.001, 33.001],
                            [-6.999 - index * 0.001, 33.001],
                            [-6.999 - index * 0.001, 33.0],
                            [-7.0 - index * 0.001, 33.0],
                        ]
                    ],
                },
            }
            for index in range(count)
        ],
    }


def _make_project(runtime: Path, *, external_additions: bool = False) -> Path:
    project_dir = runtime / "temporal_projects" / "temporal-test"
    request_dir = runtime / "requests" / "request-hash-2026"
    _write_json(request_dir / "manifest.json", {"request_hash": "request-hash-2026"})
    _write_file(request_dir / "prediction_change_mask.tif", b"mask")
    _write_file(runtime / "wayback_mosaics" / "cache-key" / "mosaic.tif", b"mosaic")
    _write_file(runtime / "imagery_cache" / "sentinel.bin", b"imagery")
    _write_file(runtime / "db_payloads" / "sentinel.json", b"{}")
    reference_cog = project_dir / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    _write_file(reference_cog, b"cog")
    additions = _feature_collection(3)
    if external_additions:
        _write_json(project_dir / "milestones" / "WB_2026_R04" / "additions.geojson", additions)
    payload = {
        "project_id": "temporal-test",
        "name": "Temporal Test",
        "project_dir": str(project_dir),
        "semantics": "expansion_only",
        "aoi_geojson": {"type": "FeatureCollection", "features": []},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "warnings": ["keep-project-warning"],
        "validation_blocking_errors": [],
        "download_bundle_path": str(request_dir / "export_bundle.zip"),
        "milestones": [
            {
                "release_identifier": "WB_2020_R04",
                "release_date": "2020-03-23",
                "status": "complete",
                "source_mode": "automated",
                "warnings": ["baseline"],
                "error_message": None,
                "pair_request_hash": None,
                "automated_additions_geojson": None,
                "automated_candidate_footprint_geojson": None,
                "automated_building_blocks_geojson": None,
                "manual_override_geojson": None,
                "additions_geojson": None,
                "effective_building_blocks_geojson": None,
                "effective_footprint_geojson": None,
                "buffer_layers_geojson": {},
                "cumulative_union_geojson": None,
                "cumulative_growth_blocks_geojson": None,
                "cumulative_growth_envelope_geojson": None,
                "reference_imagery": None,
                "metrics": None,
                "artifacts": [],
            },
            {
                "release_identifier": "WB_2026_R04",
                "release_date": "2026-04-30",
                "status": "complete",
                "source_mode": "automated",
                "warnings": ["keep-milestone-warning"],
                "error_message": None,
                "pair_request_hash": "request-hash-2026",
                "automated_additions_geojson": None,
                "automated_candidate_footprint_geojson": None,
                "automated_building_blocks_geojson": None,
                "manual_override_geojson": None,
                "additions_geojson": additions,
                "effective_building_blocks_geojson": None,
                "effective_footprint_geojson": None,
                "buffer_layers_geojson": {"10m": _feature_collection(2)},
                "cumulative_union_geojson": _feature_collection(2),
                "cumulative_growth_blocks_geojson": None,
                "cumulative_growth_envelope_geojson": None,
                "reference_imagery": {
                    "image_path": None,
                    "image_png_data_url": None,
                    "raster_bounds_wgs84": [-7.0, 33.0, -6.9, 33.1],
                    "storage_strategy": "cog",
                    "cog_path": str(reference_cog),
                    "cog_url": None,
                    "tilejson_url": "/api/reference/tilejson.json",
                    "tiles_url_template": "/api/reference/{z}/{x}/{y}.png",
                    "minzoom": 0,
                    "maxzoom": 18,
                    "tile_size": 256,
                    "reference_imagery_key": "canonical-key",
                    "canonical_cog_path": str(reference_cog),
                    "materialization_method": "canonical_cache",
                },
                "metrics": {"added_area_m2": 12.0, "total_area_m2": 20.0},
                "artifacts": [],
            },
        ],
    }
    _write_json(project_dir / "project.json", payload)
    _write_json(project_dir / "project_manifest.json", payload)
    _write_json(project_dir / "project_summary.json", {"project_id": "temporal-test", "name": "Temporal Test"})
    return project_dir


def _read_project(project_dir: Path, name: str = "project.json") -> dict:
    return json.loads((project_dir / name).read_text(encoding="utf-8"))


def _snapshot(root: Path) -> set[tuple[str, int]]:
    return {(str(path.relative_to(root)), path.stat().st_size) for path in root.rglob("*") if path.is_file()}


def test_dry_run_does_not_mutate_metadata_or_files(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)
    before = _snapshot(runtime)

    report = module.build_report(
        runtime,
        project_id="temporal-test",
        apply=False,
        yes=False,
        backup=False,
        max_inline_geojson_bytes=100,
        max_rows=100,
    )

    assert report["mode"] == "dry_run"
    assert report["summary"]["artifacts_externalized_count"] >= 3
    assert _snapshot(runtime) == before
    assert _read_project(project_dir)["milestones"][1]["additions_geojson"] is not None


def test_apply_without_yes_does_not_mutate(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)
    before = _snapshot(runtime)

    code = module.main(["--runtime-cache-dir", str(runtime), "--project-id", "temporal-test", "--apply", "--max-inline-geojson-bytes", "100", "--json"])

    assert code == 2
    assert _snapshot(runtime) == before
    assert _read_project(project_dir)["milestones"][1]["additions_geojson"] is not None


def test_apply_externalizes_inline_additions_and_updates_artifact_reference(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)

    report = module.build_report(
        runtime,
        project_id="temporal-test",
        apply=True,
        yes=True,
        backup=True,
        max_inline_geojson_bytes=100,
        max_rows=100,
    )

    additions_path = project_dir / "milestones" / "WB_2026_R04" / "additions.geojson"
    payload = _read_project(project_dir)
    milestone = payload["milestones"][1]
    artifact = next(item for item in milestone["artifacts"] if item["key"] == "additions")
    assert additions_path.is_file()
    assert milestone["additions_geojson"] is None
    assert artifact["path"] == str(additions_path)
    assert artifact["geojson_url"].endswith("/artifacts/additions.geojson")
    assert artifact["download_url"].endswith("/artifacts/additions.geojson")
    assert artifact["feature_count"] == 3
    assert artifact["qgis_compatible"] is True
    assert report["backups"]


def test_existing_external_artifact_is_reused_without_overwrite(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime, external_additions=True)
    additions_path = project_dir / "milestones" / "WB_2026_R04" / "additions.geojson"
    before_text = additions_path.read_text(encoding="utf-8")

    report = module.build_report(
        runtime,
        project_id="temporal-test",
        apply=True,
        yes=True,
        backup=False,
        max_inline_geojson_bytes=100,
        max_rows=100,
    )

    assert additions_path.read_text(encoding="utf-8") == before_text
    assert any(item["artifact_key"] == "additions" for item in report["artifacts_reused"])


def test_reference_imagery_pair_hash_metrics_and_warnings_are_preserved(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)

    module.build_report(runtime, project_id="temporal-test", apply=True, yes=True, backup=False, max_inline_geojson_bytes=100, max_rows=100)

    milestone = _read_project(project_dir)["milestones"][1]
    assert milestone["pair_request_hash"] == "request-hash-2026"
    assert milestone["reference_imagery"]["cog_path"].endswith("reference_imagery_cog.tif")
    assert milestone["reference_imagery"]["canonical_cog_path"].endswith("reference_imagery_cog.tif")
    assert milestone["reference_imagery"]["reference_imagery_key"] == "canonical-key"
    assert milestone["warnings"] == ["keep-milestone-warning"]
    assert milestone["metrics"]["added_area_m2"] == 12.0
    assert milestone["status"] == "complete"
    assert milestone["release_date"] == "2026-04-30"


def test_small_inline_geojson_is_preserved_when_under_threshold(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)

    module.build_report(runtime, project_id="temporal-test", apply=True, yes=True, backup=False, max_inline_geojson_bytes=1_000_000, max_rows=100)

    milestone = _read_project(project_dir)["milestones"][1]
    assert milestone["additions_geojson"] is not None
    assert not (project_dir / "milestones" / "WB_2026_R04" / "additions.geojson").exists()


def test_project_and_manifest_are_both_compacted(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)

    module.build_report(runtime, project_id="temporal-test", apply=True, yes=True, backup=False, max_inline_geojson_bytes=100, max_rows=100)

    assert _read_project(project_dir, "project.json")["milestones"][1]["additions_geojson"] is None
    assert _read_project(project_dir, "project_manifest.json")["milestones"][1]["additions_geojson"] is None


def test_schema_model_accepts_compacted_payload(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    project_dir = _make_project(runtime)

    module.build_report(runtime, project_id="temporal-test", apply=True, yes=True, backup=False, max_inline_geojson_bytes=100, max_rows=100)

    from src.schemas import TemporalProject

    TemporalProject.model_validate(_read_project(project_dir))


def test_compaction_does_not_delete_runtime_cache_sentinels(tmp_path: Path) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    _make_project(runtime)
    protected = [
        runtime / "requests" / "request-hash-2026" / "prediction_change_mask.tif",
        runtime / "wayback_mosaics" / "cache-key" / "mosaic.tif",
        runtime / "imagery_cache" / "sentinel.bin",
        runtime / "db_payloads" / "sentinel.json",
    ]

    module.build_report(runtime, project_id="temporal-test", apply=True, yes=True, backup=False, max_inline_geojson_bytes=100, max_rows=100)

    assert all(path.exists() for path in protected)


def test_json_and_markdown_output_shapes(tmp_path: Path, capsys) -> None:
    module = _load_compact_module()
    runtime = tmp_path / "runtime_cache"
    _make_project(runtime)

    assert module.main(["--runtime-cache-dir", str(runtime), "--project-id", "temporal-test", "--dry-run", "--json", "--max-inline-geojson-bytes", "100"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["project_count"] == 1

    report = module.build_report(runtime, project_id="temporal-test", apply=False, yes=False, backup=False, max_inline_geojson_bytes=100, max_rows=100)
    markdown = module.render_markdown(report, max_rows=100)
    for section in module.REQUIRED_MARKDOWN_SECTIONS:
        assert f"## {section}" in markdown


def test_phase0_and_phase2_can_scan_compacted_metadata(tmp_path: Path) -> None:
    compact = _load_compact_module()
    inspect = _load_module(INSPECT_SCRIPT, "inspect_runtime_storage_for_compaction_test")
    cleanup = _load_module(CLEANUP_SCRIPT, "cleanup_runtime_storage_for_compaction_test")
    runtime = tmp_path / "runtime_cache"
    _make_project(runtime)

    compact.build_report(runtime, project_id="temporal-test", apply=True, yes=True, backup=False, max_inline_geojson_bytes=100, max_rows=100)

    audit = inspect.build_audit(runtime)
    project = next(item for item in audit["temporal_projects"] if item["project_id"] == "temporal-test")
    assert project["large_inline_json_risk"] is False
    cleanup_report = cleanup.build_report(runtime, apply=False, yes=False, older_than_hours=72, active_window_hours=24, max_rows=100)
    assert not any(item.get("reason") == "large_project_metadata_risk" for item in cleanup_report["unknown_risk_items"])
    protected = next(item for item in cleanup_report["protected_requests"] if item["request_hash"] == "request-hash-2026")
    assert "recent_or_active_request" in protected["reason"]
    assert any(ref["reason"] == "pair_request_hash_reference" for ref in protected["source_references"])
