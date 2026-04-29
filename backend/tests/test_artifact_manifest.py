from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
import pytest
from rasterio.transform import from_bounds

from src.config import Settings
from src.domain.artifact_manifest import (
    build_manifest,
    iter_artifacts_by_type,
    iter_exportable_artifacts,
    read_manifest,
    resolve_artifact_path,
    write_manifest_atomic,
)
from src.domain.exports import create_export_bundle_from_manifest, export_bandon_outputs
from src.domain.run_workspace import cleanup_run_tmp_dir, get_run_tmp_dir


def _write_raster(path, data: np.ndarray) -> None:
    height, width = data.shape[:2]
    count = data.shape[2] if data.ndim == 3 else 1
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=count,
        dtype=data.dtype,
        crs="EPSG:3857",
        transform=from_bounds(-8, 33, -7, 34, width, height),
    ) as dst:
        if data.ndim == 3:
            for index in range(count):
                dst.write(data[:, :, index], index + 1)
        else:
            dst.write(data, 1)


def _empty_feature_collection() -> dict:
    return {"type": "FeatureCollection", "features": []}


def test_manifest_round_trip_and_shared_cache_resolution(tmp_path) -> None:
    runtime_cache_dir = tmp_path / "runtime_cache"
    request_dir = runtime_cache_dir / "requests" / "run-1"
    request_dir.mkdir(parents=True)
    shared_dir = runtime_cache_dir / "wayback_mosaics" / "cache-123"
    shared_dir.mkdir(parents=True)
    tmp_dir = runtime_cache_dir / "tmp" / "run-1"
    tmp_dir.mkdir(parents=True)

    final_path = request_dir / "building_change_blocks.geojson"
    final_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    preview_path = request_dir / "t1_preview.png"
    preview_path.write_bytes(b"preview")
    shared_source_path = shared_dir / "mosaic.tif"
    shared_source_path.write_bytes(b"source")
    shared_mask_path = shared_dir / "valid_mask.tif"
    shared_mask_path.write_bytes(b"mask")
    (request_dir / "run_response.json").write_text("{}", encoding="utf-8")
    (tmp_dir / "bandon_input_t1.png").write_bytes(b"tmp")

    manifest = build_manifest(
        "run-1",
        request_dir,
        [
            {"path": str(final_path)},
            {"path": str(preview_path)},
            {
                "path": str(shared_source_path),
                "resolved_path": str(shared_source_path),
                "artifact_type": "source",
                "purpose": "shared source raster",
                "format": "tif",
                "keep_policy": "cache",
                "include_in_export": False,
                "storage": "shared_cache",
                "cache_key": "cache-123",
            },
            {
                "path": str(shared_mask_path),
                "resolved_path": str(shared_mask_path),
                "artifact_type": "source",
                "purpose": "shared source valid mask",
                "format": "tif",
                "keep_policy": "cache",
                "include_in_export": False,
                "storage": "shared_cache",
                "cache_key": "cache-123",
            },
        ],
    )
    write_manifest_atomic(request_dir, manifest)

    loaded = read_manifest(request_dir)
    assert loaded is not None
    exportable = iter_exportable_artifacts(request_dir)
    source_paths = iter_artifacts_by_type(request_dir, "source")

    assert exportable == [final_path, preview_path]
    assert shared_source_path in source_paths
    shared_entry = next(item for item in loaded["artifacts"] if item.get("cache_key") == "cache-123")
    assert shared_entry["storage"] == "shared_cache"
    assert resolve_artifact_path(request_dir, shared_entry) == shared_source_path


def test_manifest_classifies_wayback_rgb_request_rasters_as_source(tmp_path) -> None:
    runtime_cache_dir = tmp_path / "runtime_cache"
    request_dir = runtime_cache_dir / "requests" / "run-wayback"
    request_dir.mkdir(parents=True)

    t1_rgb_path = request_dir / "t1_wayback_rgb.tif"
    t2_rgb_path = request_dir / "t2_wayback_rgb.tif"
    source_rgb_path = request_dir / "source_wayback_rgb.tif"
    final_path = request_dir / "building_change_blocks.geojson"
    preview_path = request_dir / "change_overlay_preview.png"

    t1_rgb_path.write_bytes(b"t1")
    t2_rgb_path.write_bytes(b"t2")
    source_rgb_path.write_bytes(b"source")
    final_path.write_text("{}", encoding="utf-8")
    preview_path.write_bytes(b"preview")

    manifest = build_manifest("run-wayback", request_dir, [])
    write_manifest_atomic(request_dir, manifest)

    loaded = read_manifest(request_dir)
    assert loaded is not None
    entries_by_name = {Path(item["path"]).name: item for item in loaded["artifacts"] if isinstance(item, dict)}

    assert entries_by_name["t1_wayback_rgb.tif"]["artifact_type"] == "source"
    assert entries_by_name["t1_wayback_rgb.tif"]["include_in_export"] is False
    assert entries_by_name["t2_wayback_rgb.tif"]["artifact_type"] == "source"
    assert entries_by_name["t2_wayback_rgb.tif"]["include_in_export"] is False
    assert entries_by_name["source_wayback_rgb.tif"]["artifact_type"] == "source"
    assert entries_by_name["source_wayback_rgb.tif"]["include_in_export"] is False

    exportable = iter_exportable_artifacts(request_dir)
    assert t1_rgb_path not in exportable
    assert t2_rgb_path not in exportable
    assert source_rgb_path not in exportable
    assert final_path in exportable
    assert preview_path in exportable

    bundle_path = create_export_bundle_from_manifest(request_dir)
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())

    assert "t1_wayback_rgb.tif" not in names
    assert "t2_wayback_rgb.tif" not in names
    assert "source_wayback_rgb.tif" not in names
    assert "building_change_blocks.geojson" in names
    assert "change_overlay_preview.png" in names


def test_export_bundle_is_explicit_and_excludes_temp_source_and_debug_files(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache")
    request_dir = settings.request_cache_dir / "run-2"
    request_dir.mkdir(parents=True)
    tmp_dir = get_run_tmp_dir(settings, "run-2")

    reference_raster_path = request_dir / "reference.tif"
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    probability = np.zeros((8, 8), dtype=np.float32)
    mask = np.zeros((8, 8), dtype=bool)
    labels = np.zeros((8, 8), dtype=np.uint16)
    _write_raster(reference_raster_path, np.zeros((8, 8), dtype=np.uint8))

    (request_dir / "t1_WB_2024_R01_z19.tif").write_bytes(b"source-t1")
    (request_dir / "t2_WB_2026_R01_z19.tif").write_bytes(b"source-t2")
    (request_dir / "t1_WB_2024_R01_z19_valid_mask.tif").write_bytes(b"mask-t1")
    (request_dir / "t2_WB_2026_R01_z19_valid_mask.tif").write_bytes(b"mask-t2")
    (tmp_dir / "bandon_input_t1.png").write_bytes(b"temp-input")
    (tmp_dir / "bandon_input_t2.png").write_bytes(b"temp-input")
    (tmp_dir / "t1_invalid_mask_for_arosics.tif").write_bytes(b"temp-mask")
    bandon_run_dir = tmp_dir / "bandon_run"
    bandon_run_dir.mkdir(parents=True)
    (bandon_run_dir / "run_metadata.json").write_text("{}", encoding="utf-8")
    (bandon_run_dir / "change_probability.npy").write_bytes(b"npy")
    (bandon_run_dir / "change_mask.png").write_bytes(b"png")

    buffer_df = pd.DataFrame([{"buffer_id": 1, "area_m2": 10.0}])
    buffer_geojson = _empty_feature_collection()

    previews, artifacts, bundle_path, _ = export_bandon_outputs(
        result_dir=request_dir,
        reference_raster_path=reference_raster_path,
        t1_rgb=rgb,
        t2_rgb=rgb,
        change_prob=probability,
        change_mask=mask,
        change_labels=labels,
        change_polygons_df=pd.DataFrame([{"change_id": 1, "area_m2": 5.0}]),
        change_polygons_geojson=_empty_feature_collection(),
        change_blocks_df=pd.DataFrame([{"block_id": 1, "area_m2": 5.0}]),
        change_blocks_geojson=_empty_feature_collection(),
        buffer_layers={"10m": (buffer_df, buffer_geojson)},
        summary_df=pd.DataFrame([{"metric": "tiles", "value": 1}]),
        bandon_metadata_path=bandon_run_dir / "run_metadata.json",
    )

    assert previews.t1_preview_path is not None
    assert bundle_path is None
    assert not (request_dir / "export_bundle.zip").exists()
    assert (request_dir / "building_change_blocks.geojson").exists()
    assert (request_dir / "building_change_buffer_10m.geojson").exists()

    created_bundle_path = create_export_bundle_from_manifest(request_dir)
    with zipfile.ZipFile(created_bundle_path) as archive:
        names = set(archive.namelist())

    assert "building_change_blocks.geojson" in names
    assert "building_change_buffer_10m.geojson" in names
    assert "t1_preview.png" in names
    assert "export_bundle.zip" not in names
    assert "manifest.json" not in names
    assert "bandon_input_t1.png" not in names
    assert "bandon_input_t2.png" not in names
    assert "run_metadata.json" not in names
    assert "change_probability.npy" not in names
    assert "t1_invalid_mask_for_arosics.tif" not in names
    assert "t1_wayback_rgb.tif" not in names
    assert "t2_wayback_rgb.tif" not in names
    assert "t1_WB_2024_R01_z19.tif" not in names
    assert "t1_WB_2024_R01_z19_valid_mask.tif" not in names
    assert any(item.name == "building_change_blocks_geojson" for item in artifacts)


def test_export_bundle_rejects_unsafe_paths_outside_runtime_roots(tmp_path) -> None:
    runtime_cache_dir = tmp_path / "runtime_cache"
    request_dir = runtime_cache_dir / "requests" / "run-unsafe"
    request_dir.mkdir(parents=True)
    safe_path = request_dir / "building_change_blocks.geojson"
    safe_path.write_text("{}", encoding="utf-8")
    external_path = tmp_path / "outside.geojson"
    external_path.write_text("{}", encoding="utf-8")

    manifest = build_manifest(
        "run-unsafe",
        request_dir,
        [
            {"path": str(safe_path)},
            {
                "path": str(external_path),
                "resolved_path": str(external_path),
                "artifact_type": "final",
                "purpose": "unsafe external artifact",
                "format": "geojson",
                "keep_policy": "always",
                "include_in_export": True,
                "storage": "external",
            },
        ],
    )
    write_manifest_atomic(request_dir, manifest)

    try:
        create_export_bundle_from_manifest(request_dir, force=True)
    except ValueError as exc:
        assert "outside runtime cache" in str(exc)
    else:
        raise AssertionError("expected unsafe export bundle creation to fail")


def test_old_request_folder_without_manifest_uses_fallback_allowlist(tmp_path) -> None:
    runtime_cache_dir = tmp_path / "runtime_cache"
    request_dir = runtime_cache_dir / "requests" / "run-legacy"
    request_dir.mkdir(parents=True)
    (request_dir / "building_change_blocks.geojson").write_text("{}", encoding="utf-8")
    (request_dir / "t1_preview.png").write_bytes(b"preview")
    (request_dir / "change_overlay_preview.png").write_bytes(b"preview")
    (request_dir / "t1_wayback_rgb.tif").write_bytes(b"source-t1")
    (request_dir / "t2_wayback_rgb.tif").write_bytes(b"source-t2")
    (request_dir / "t1_WB_2024_R01_z19_valid_mask.tif").write_bytes(b"mask-t1")
    (request_dir / "t2_WB_2026_R01_z19_valid_mask.tif").write_bytes(b"mask-t2")
    (request_dir / "run_metadata.json").write_text("{}", encoding="utf-8")
    (request_dir / "bandon_input_t1.png").write_bytes(b"temp")
    (request_dir / "nested.zip").write_bytes(b"zip")

    bundle_path = create_export_bundle_from_manifest(request_dir)
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())

    assert "building_change_blocks.geojson" in names
    assert "t1_preview.png" in names
    assert "change_overlay_preview.png" in names
    assert "run_metadata.json" not in names
    assert "bandon_input_t1.png" not in names
    assert "t1_wayback_rgb.tif" not in names
    assert "t2_wayback_rgb.tif" not in names
    assert "t1_WB_2024_R01_z19_valid_mask.tif" not in names
    assert "t2_WB_2026_R01_z19_valid_mask.tif" not in names
    assert "nested.zip" not in names


def test_force_export_rebuild_replaces_stale_legacy_bundle(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache")
    request_dir = settings.request_cache_dir / "run-force-rebuild"
    request_dir.mkdir(parents=True)
    tmp_dir = get_run_tmp_dir(settings, "run-force-rebuild")

    final_path = request_dir / "building_change_blocks.geojson"
    preview_path = request_dir / "t1_preview.png"
    source_path = request_dir / "t1_wayback_rgb.tif"
    temp_path = tmp_dir / "bandon_input_t1.png"

    final_path.write_text("{}", encoding="utf-8")
    preview_path.write_bytes(b"preview")
    source_path.write_bytes(b"source")
    temp_path.write_bytes(b"temp")

    manifest = build_manifest("run-force-rebuild", request_dir, [])
    write_manifest_atomic(request_dir, manifest)

    stale_bundle = request_dir / "export_bundle.zip"
    with zipfile.ZipFile(stale_bundle, "w") as archive:
        archive.write(source_path, arcname=source_path.name)
        archive.write(temp_path, arcname=temp_path.name)

    rebuilt_bundle = create_export_bundle_from_manifest(request_dir, force=True)
    with zipfile.ZipFile(rebuilt_bundle) as archive:
        names = set(archive.namelist())

    assert "building_change_blocks.geojson" in names
    assert "t1_preview.png" in names
    assert "t1_wayback_rgb.tif" not in names
    assert "bandon_input_t1.png" not in names


def test_legacy_request_folder_without_final_outputs_fails_cleanly(tmp_path) -> None:
    runtime_cache_dir = tmp_path / "runtime_cache"
    request_dir = runtime_cache_dir / "requests" / "run-empty"
    request_dir.mkdir(parents=True)
    (request_dir / "t1_wayback_rgb.tif").write_bytes(b"source-t1")
    (request_dir / "t2_wayback_rgb.tif").write_bytes(b"source-t2")
    (request_dir / "t1_WB_2024_R01_z19_valid_mask.tif").write_bytes(b"mask-t1")
    (request_dir / "bandon_input_t1.png").write_bytes(b"temp")

    with pytest.raises(ValueError, match="No exportable final artifacts found"):
        create_export_bundle_from_manifest(request_dir)


def test_cleanup_run_tmp_dir_deletes_after_success_when_debug_disabled(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache", keep_intermediate_artifacts=False)
    tmp_dir = get_run_tmp_dir(settings, "run-clean")
    (tmp_dir / "debug.txt").write_text("debug", encoding="utf-8")

    cleanup_run_tmp_dir(settings, "run-clean", success=True)

    assert not tmp_dir.exists()


def test_cleanup_run_tmp_dir_preserves_after_success_when_debug_enabled(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache", keep_intermediate_artifacts=True)
    tmp_dir = get_run_tmp_dir(settings, "run-keep")
    (tmp_dir / "debug.txt").write_text("debug", encoding="utf-8")

    cleanup_run_tmp_dir(settings, "run-keep", success=True)

    assert tmp_dir.exists()


def test_cleanup_run_tmp_dir_preserves_after_failure(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache", keep_intermediate_artifacts=False)
    tmp_dir = get_run_tmp_dir(settings, "run-failed")
    (tmp_dir / "debug.txt").write_text("debug", encoding="utf-8")

    cleanup_run_tmp_dir(settings, "run-failed", success=False)

    assert tmp_dir.exists()


def test_cleanup_script_dry_run_and_apply(tmp_path) -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_runtime_cache.py"
    spec = importlib.util.spec_from_file_location("cleanup_runtime_cache", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    settings = Settings(runtime_cache_dir=tmp_path / "runtime_cache")
    tmp_dir = get_run_tmp_dir(settings, "old-run")
    tmp_file = tmp_dir / "debug.bin"
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_bytes(b"12345")
    export_bundle = settings.request_cache_dir / "old-run" / "export_bundle.zip"
    export_bundle.parent.mkdir(parents=True, exist_ok=True)
    export_bundle.write_bytes(b"zip")
    final_artifact = settings.request_cache_dir / "old-run" / "building_change_blocks.geojson"
    final_artifact.write_text("{}", encoding="utf-8")

    old_mtime = 1_600_000_000
    for path in (tmp_dir, tmp_file, export_bundle, final_artifact.parent, final_artifact):
        path.touch(exist_ok=True)
    for path in (tmp_dir, tmp_file, export_bundle, final_artifact.parent, final_artifact):
        path.chmod(path.stat().st_mode)

    import os

    os.utime(tmp_file, (old_mtime, old_mtime))
    os.utime(export_bundle, (old_mtime, old_mtime))
    os.utime(final_artifact, (old_mtime, old_mtime))
    os.utime(final_artifact.parent, (old_mtime, old_mtime))
    os.utime(tmp_dir, (old_mtime, old_mtime))

    candidates = module.collect_cleanup_candidates(
        settings,
        older_than_days=1,
        include_exports=True,
        include_tmp=True,
        include_old_auto_bundles=True,
        include_wayback_cache=False,
    )
    candidate_paths = {item.path for item in candidates}
    assert tmp_dir in candidate_paths
    assert export_bundle in candidate_paths
    assert final_artifact not in candidate_paths

    deleted, freed = module.apply_cleanup(candidates, destructive=False)
    assert deleted == 0
    assert freed == 0
    assert tmp_dir.exists()
    assert export_bundle.exists()

    deleted, freed = module.apply_cleanup(candidates, destructive=True)
    assert deleted >= 2
    assert freed >= len(b"12345") + len(b"zip")
    assert not tmp_dir.exists()
    assert not export_bundle.exists()
    assert final_artifact.exists()


def test_cleanup_script_runs_directly_without_pythonpath(tmp_path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    runtime_cache_dir = tmp_path / "runtime_cache"
    tmp_dir = runtime_cache_dir / "tmp" / "old-run"
    tmp_dir.mkdir(parents=True)
    (tmp_dir / "debug.bin").write_bytes(b"12345")
    export_bundle = runtime_cache_dir / "requests" / "old-run" / "export_bundle.zip"
    export_bundle.parent.mkdir(parents=True, exist_ok=True)
    export_bundle.write_bytes(b"zip")

    env = os.environ.copy()
    env["APP_RUNTIME_CACHE_DIR"] = str(runtime_cache_dir)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/cleanup_runtime_cache.py",
            "--dry-run",
            "--older-than-days",
            "0",
            "--include-tmp",
            "--include-exports",
        ],
        cwd=backend_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "files matched:" in result.stdout
    assert str(tmp_dir) in result.stdout
    assert str(export_bundle) in result.stdout
    assert tmp_dir.exists()
    assert export_bundle.exists()
