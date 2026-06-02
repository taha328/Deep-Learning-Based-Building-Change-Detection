from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from src.domain.reference_imagery_cache import (
    build_reference_imagery_cache_key_payload,
    build_reference_imagery_cache_metadata,
    build_reference_imagery_key,
    reference_imagery_cache_cog_path,
    reference_imagery_cache_metadata_path,
    write_reference_imagery_cache_metadata,
)
from src.services.temporal_reference_imagery import REFERENCE_COG_FORMAT_VERSION, ensure_reference_imagery_cog


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_canonical_cog_inference_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_canonical_cog_inference_smoke", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_rgb(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=256,
        height=256,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(0.0, 0.0, 256.0, 256.0, width=256, height=256),
    ) as dst:
        dst.write(np.ones((3, 256, 256), dtype=np.uint8))
    return path


def _write_mask(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=256,
        height=256,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(0.0, 0.0, 256.0, 256.0, width=256, height=256),
    ) as dst:
        dst.write(np.ones((256, 256), dtype=np.uint8), 1)
    return path


def _canonical(runtime: Path, release_identifier: str, *, aoi_hash: str = "shared-aoi") -> Path:
    payload = build_reference_imagery_cache_key_payload(
        provider="esri_wayback",
        release_identifier=release_identifier,
        release_num=1,
        tile_matrix_set="default028mm",
        zoom=18,
        tile_range=[0, 0, 0, 0],
        bounds_3857=[0.0, 0.0, 256.0, 256.0],
        source_raster_path=None,
        valid_mask_path=None,
        aoi_hash=aoi_hash,
        reference_cog_format_version=REFERENCE_COG_FORMAT_VERSION,
    )
    reference_key = build_reference_imagery_key(payload)
    canonical = reference_imagery_cache_cog_path(runtime / "imagery_cache", reference_key)
    metadata_path = reference_imagery_cache_metadata_path(runtime / "imagery_cache", reference_key)
    source = _write_rgb(runtime / "source" / f"{release_identifier}.tif")
    mask = _write_mask(runtime / "source" / f"{release_identifier}_mask.tif")
    ensure_reference_imagery_cog(source, canonical, valid_mask_path=mask, release_identifier=release_identifier)
    _write_mask(canonical.with_name("valid_mask.tif"))
    metadata = build_reference_imagery_cache_metadata(
        reference_imagery_key=reference_key,
        key_payload=payload,
        canonical_cog_path=canonical,
    )
    write_reference_imagery_cache_metadata(metadata_path, metadata)
    return metadata_path


def test_select_smallest_pair_uses_shared_aoi_canonical_cogs(tmp_path: Path) -> None:
    module = _load_module()
    runtime = tmp_path / "runtime_cache"
    _canonical(runtime, "WB_2020_R04")
    _canonical(runtime, "WB_2026_R04")

    t1, t2 = module.select_smallest_pair(runtime)

    assert t1.metadata["aoi_hash"] == "shared-aoi"
    assert t2.metadata["aoi_hash"] == "shared-aoi"
    assert t1.metadata["release_identifier"] != t2.metadata["release_identifier"]
    assert t1.canonical_cog_path.is_file()
    assert t2.valid_mask_path.is_file()

