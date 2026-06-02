#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys

import rasterio

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.domain.inference_reference_imagery import validate_canonical_cog_for_inference  # noqa: E402
from src.domain.reference_imagery_cache import read_reference_imagery_cache_metadata  # noqa: E402
from src.domain.tiled_inference import (  # noqa: E402
    TiledInferenceConfig,
    make_bandon_patch_predictor,
    run_tiled_inference,
)


@dataclass(frozen=True)
class CanonicalEntry:
    metadata_path: Path
    metadata: dict[str, Any]
    canonical_cog_path: Path
    valid_mask_path: Path
    pixel_count: int


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def smoke_settings(runtime_cache_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=REPO_ROOT,
        runtime_cache_dir=runtime_cache_dir,
        inference_backend=os.getenv("APP_INFERENCE_BACKEND", "bandon_mps"),
        inference_tiled_mode_auto=True,
        inference_tile_size=_int_env("APP_INFERENCE_TILE_SIZE", 1024),
        inference_tile_overlap=_int_env("APP_INFERENCE_TILE_OVERLAP", 128),
        inference_tile_batch_size=_int_env("APP_INFERENCE_TILE_BATCH_SIZE", 1),
        inference_max_in_memory_pixels=_int_env("APP_INFERENCE_MAX_IN_MEMORY_PIXELS", 25_000_000),
        inference_heavy_batch_tile_threshold=_int_env("APP_INFERENCE_HEAVY_BATCH_TILE_THRESHOLD", 2000),
        default_change_threshold=_float_env("APP_CHANGE_THRESHOLD", 0.35),
        bandon_repo_dir=REPO_ROOT / "vendor" / "BANDON-mps",
        bandon_env_prefix=REPO_ROOT / "vendor" / "BANDON-mps" / ".conda-macos-mps",
        bandon_config_path=REPO_ROOT / "vendor" / "BANDON-mps" / "workdirs_bandon" / "MTGCDNet" / "config.py",
        bandon_checkpoint_path=REPO_ROOT / "vendor" / "BANDON-mps" / "checkpoints" / "mtgcdnet_iter_40000.pth",
        bandon_device=os.getenv("APP_BANDON_DEVICE", "mps"),
        bandon_allow_mps_fallback=_bool_env("APP_BANDON_ALLOW_MPS_FALLBACK", False),
        bandon_skip_invalid_crops=True,
        bandon_skip_outside_aoi_crops=True,
        bandon_skip_nodata_crops=True,
        bandon_min_valid_ratio_within_aoi=_float_env("APP_BANDON_MIN_VALID_RATIO_WITHIN_AOI", 0.01),
    )


def expected_payload(metadata: dict[str, Any]) -> dict[str, object]:
    return {
        key: metadata.get(key)
        for key in (
            "provider",
            "release_identifier",
            "release_num",
            "tile_matrix_set",
            "zoom",
            "tile_range",
            "bounds_3857",
            "aoi_hash",
            "reference_cog_format_version",
        )
    }


def load_entry(metadata_path: Path) -> CanonicalEntry | None:
    metadata = read_reference_imagery_cache_metadata(metadata_path)
    if not metadata:
        return None
    canonical_path = metadata.get("canonical_cog_path")
    reference_key = metadata.get("reference_imagery_key")
    if not isinstance(canonical_path, str) or not isinstance(reference_key, str):
        return None
    cog_path = Path(canonical_path)
    valid_mask_path = cog_path.with_name("valid_mask.tif")
    if not cog_path.is_file() or not valid_mask_path.is_file():
        return None
    validation = validate_canonical_cog_for_inference(
        canonical_cog_path=cog_path,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=expected_payload(metadata),
        normalized_aoi=None,
    )
    if not validation.valid:
        return None
    with rasterio.open(cog_path) as src:
        pixel_count = int(src.width * src.height)
    return CanonicalEntry(metadata_path=metadata_path, metadata=metadata, canonical_cog_path=cog_path, valid_mask_path=valid_mask_path, pixel_count=pixel_count)


def select_smallest_pair(runtime_cache_dir: Path) -> tuple[CanonicalEntry, CanonicalEntry]:
    groups: dict[str, list[CanonicalEntry]] = defaultdict(list)
    for metadata_path in sorted((runtime_cache_dir / "imagery_cache").glob("*/metadata.json")):
        entry = load_entry(metadata_path)
        if entry is None:
            continue
        aoi_hash = entry.metadata.get("aoi_hash")
        if isinstance(aoi_hash, str) and aoi_hash:
            groups[aoi_hash].append(entry)
    candidates: list[tuple[int, CanonicalEntry, CanonicalEntry]] = []
    for rows in groups.values():
        rows = sorted(rows, key=lambda item: (str(item.metadata.get("release_identifier")), item.pixel_count))
        for left_index, left in enumerate(rows):
            for right in rows[left_index + 1 :]:
                if left.metadata.get("release_identifier") == right.metadata.get("release_identifier"):
                    continue
                candidates.append((left.pixel_count + right.pixel_count, left, right))
    if not candidates:
        raise RuntimeError("no_valid_canonical_cog_pair_with_shared_aoi_hash")
    _pixels, t1, t2 = min(candidates, key=lambda item: item[0])
    return t1, t2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded real-model smoke through canonical COG imagery.")
    parser.add_argument("--runtime-cache-dir", type=Path, default=BACKEND_ROOT / "runtime_cache")
    parser.add_argument("--out-dir", type=Path, default=BACKEND_ROOT / "runtime_cache" / "phase36_canonical_cog_inference_smoke")
    parser.add_argument("--run-id", default="phase36-canonical-cog-smoke")
    parser.add_argument("--max-tiles", type=int, default=1)
    parser.add_argument("--json", action="store_true", default=True)
    args = parser.parse_args()

    runtime_cache_dir = args.runtime_cache_dir.expanduser().resolve()
    settings = smoke_settings(runtime_cache_dir)
    checkpoint = Path(settings.bandon_checkpoint_path)
    if not checkpoint.is_file():
        print(json.dumps({"success": False, "reason": "missing_bandon_checkpoint", "checkpoint_path": str(checkpoint)}, indent=2))
        return 2
    t1, t2 = select_smallest_pair(runtime_cache_dir)
    predictor = make_bandon_patch_predictor(
        settings=settings,
        effective_backend=settings.inference_backend,
        threshold=settings.default_change_threshold,
    )
    result = run_tiled_inference(
        t1_mosaic_path=t1.canonical_cog_path,
        t2_mosaic_path=t2.canonical_cog_path,
        t1_valid_mask_path=t1.valid_mask_path,
        t2_valid_mask_path=t2.valid_mask_path,
        output_dir=args.out_dir,
        run_id=args.run_id,
        settings=settings,
        config=TiledInferenceConfig.from_settings(settings, threshold=settings.default_change_threshold),
        predictor=predictor,
        release_t1=str(t1.metadata.get("release_identifier")),
        release_t2=str(t2.metadata.get("release_identifier")),
        max_tiles=args.max_tiles,
    )
    response = {
        "success": True,
        "backend": settings.inference_backend,
        "checkpoint_path": str(checkpoint),
        "imagery_source_mode": "canonical_cog",
        "t1": {
            "release_identifier": t1.metadata.get("release_identifier"),
            "reference_imagery_key": t1.metadata.get("reference_imagery_key"),
            "canonical_cog_path": str(t1.canonical_cog_path),
            "metadata_path": str(t1.metadata_path),
        },
        "t2": {
            "release_identifier": t2.metadata.get("release_identifier"),
            "reference_imagery_key": t2.metadata.get("reference_imagery_key"),
            "canonical_cog_path": str(t2.canonical_cog_path),
            "metadata_path": str(t2.metadata_path),
        },
        "result": {
            "run_id": args.run_id,
            "processed_tiles": result.processed_tiles,
            "total_tiles": result.total_tiles,
            "feature_count": result.feature_count,
            "probability_path": str(result.probability_path),
            "mask_path": str(result.mask_path),
            "geojsonl_path": str(result.geojsonl_path),
            "metadata_path": str(result.metadata_path),
            "probability_stats": result.probability_stats,
        },
    }
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
