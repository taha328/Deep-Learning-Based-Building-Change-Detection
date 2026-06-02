from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "inspect_runtime_storage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("inspect_runtime_storage", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_runtime_tree(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime_cache"
    referenced_request = runtime / "requests" / "run-referenced"
    orphan_request = runtime / "requests" / "run-orphan"
    referenced_request.mkdir(parents=True)
    orphan_request.mkdir(parents=True)
    (referenced_request / "run_response.json").write_text("{}", encoding="utf-8")
    (referenced_request / "manifest.json").write_text("{}", encoding="utf-8")
    (referenced_request / "export_bundle.zip").write_bytes(b"zip")
    (orphan_request / "run_response.json").write_text("{}", encoding="utf-8")

    project_dir = runtime / "temporal_projects" / "temporal-test"
    cog_path = project_dir / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"
    cog_path.parent.mkdir(parents=True)
    cog_path.write_bytes(b"cog")
    artifact_path = project_dir / "milestones" / "WB_2026_R04" / "additions.geojson"
    artifact_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    missing_cog = project_dir / "milestones" / "WB_2024_R02" / "reference_imagery_cog.tif"
    _write_json(
        project_dir / "project.json",
        {
            "project_id": "temporal-test",
            "project_dir": str(project_dir),
            "milestones": [
                {
                    "release_identifier": "WB_2026_R04",
                    "pair_request_hash": "run-referenced",
                    "reference_imagery": {"cog_path": str(cog_path)},
                    "artifacts": [{"key": "additions", "path": str(artifact_path)}],
                },
                {
                    "release_identifier": "WB_2024_R02",
                    "reference_imagery": {"cog_path": str(missing_cog)},
                    "artifacts": [],
                },
            ],
        },
    )
    _write_json(project_dir / "project_summary.json", {"project_id": "temporal-test"})

    for folder in ("reference_tiles", "temporal_vector_tiles", "qgis_artifacts"):
        path = runtime / folder / "cached.file"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"cache")

    tmp_file = runtime / "tmp" / "run" / "scratch.bin"
    tmp_file.parent.mkdir(parents=True)
    tmp_file.write_bytes(b"tmp")

    log_file = runtime / "dev_client_logs" / "client_log.ndjson"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("{}", encoding="utf-8")

    db_payload = runtime / "db_payloads" / "runs" / "raw_response" / "payload.json"
    db_payload.parent.mkdir(parents=True)
    db_payload.write_text("{}", encoding="utf-8")

    wayback = runtime / "wayback_mosaics" / "cache-key"
    wayback.mkdir(parents=True)
    (wayback / "mosaic.tif").write_bytes(b"mosaic")
    (wayback / "mosaic.png").write_bytes(b"png")
    (wayback / "valid_mask.tif").write_bytes(b"mask")
    _write_json(
        wayback / "metadata.json",
        {
            "release_identifier": "WB_2026_R04",
            "zoom": 18,
            "tile_range": [1, 2, 3, 4],
            "width": 512,
            "height": 512,
            "tile_count": 4,
        },
    )
    return runtime


def test_referenced_request_folder_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)

    referenced = next(item for item in audit["requests"] if item["request_hash"] == "run-referenced")
    assert referenced["referenced_by_pair_request_hash"] is True
    assert referenced["orphan_candidate"] is False
    assert referenced["classification"] == "protected_reference"


def test_unreferenced_request_folder_is_orphan_candidate(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)

    orphan = next(item for item in audit["requests"] if item["request_hash"] == "run-orphan")
    assert orphan["orphan_candidate"] is True
    assert orphan["classification"] == "orphan_candidate"


def test_existing_referenced_cog_is_protected(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)
    protected_paths = {item["path"] for item in audit["temporal_project_references"]["protected"]}

    assert any(path.endswith("WB_2026_R04/reference_imagery_cog.tif") for path in protected_paths)


def test_missing_referenced_cog_is_reported(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)
    missing_paths = {item["path"] for item in audit["missing_references"]}

    assert any(path.endswith("WB_2024_R02/reference_imagery_cog.tif") for path in missing_paths)


def test_derived_caches_are_classified_as_rebuildable(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)
    by_name = {item["name"]: item for item in audit["derived_caches"]}

    assert by_name["reference_tiles"]["classification"] == "derived_rebuildable_cache"
    assert by_name["temporal_vector_tiles"]["classification"] == "derived_rebuildable_cache"
    assert by_name["qgis_artifacts"]["classification"] == "derived_rebuildable_cache"


def test_wayback_mosaics_are_not_safe_delete_candidates(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)

    assert audit["wayback_mosaics"]
    assert all(item["classification"] == "canonical_reusable_cache" for item in audit["wayback_mosaics"])
    assert not any(str(candidate["path"]).startswith(str(runtime / "wayback_mosaics")) for candidate in audit["orphan_candidates"])


def test_db_payloads_are_unknown_risk(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)

    assert audit["db_payloads"]["classification"] == "unknown_risk"


def test_markdown_output_contains_required_sections(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    markdown = module.render_markdown(module.build_audit(runtime), max_rows=20)

    for section in module.REQUIRED_MARKDOWN_SECTIONS:
        assert f"## {section}" in markdown


def test_json_output_shape_is_valid(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)

    audit = module.build_audit(runtime)
    encoded = json.dumps(audit)
    decoded = json.loads(encoded)

    for key in (
        "runtime_cache_dir",
        "summary",
        "largest_directories",
        "largest_files",
        "temporal_project_references",
        "requests",
        "wayback_mosaics",
        "temporal_projects",
        "reference_imagery_cogs",
        "derived_caches",
        "db_payloads",
        "missing_references",
        "orphan_candidates",
        "protected_artifacts",
        "risk_notes",
        "suggested_next_actions",
    ):
        assert key in decoded


def test_script_does_not_modify_fake_runtime_tree(tmp_path: Path) -> None:
    module = _load_module()
    runtime = _make_runtime_tree(tmp_path)
    before = sorted((path.relative_to(runtime), path.stat().st_size) for path in runtime.rglob("*") if path.is_file())

    module.build_audit(runtime)

    after = sorted((path.relative_to(runtime), path.stat().st_size) for path in runtime.rglob("*") if path.is_file())
    assert after == before


def test_script_source_has_no_runtime_mutation_calls() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    forbidden = (
        ".unlink(",
        ".remove(",
        ".rmdir(",
        ".rename(",
        ".replace(",
        ".write_text(",
        ".write_bytes(",
        "rmtree(",
        "shutil.move(",
        "shutil.copy",
        "os.remove(",
        "os.unlink(",
        "\"w\"",
        "\"a\"",
        "'w'",
        "'a'",
    )
    assert not any(token in source for token in forbidden)
