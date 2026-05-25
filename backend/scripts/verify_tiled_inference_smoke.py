from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import rasterio

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.domain.tiled_inference import (  # noqa: E402
    TiledInferenceConfig,
    make_bandon_patch_predictor,
    make_difference_patch_predictor,
    make_synthetic_square_patch_predictor,
    run_tiled_inference,
    select_inference_mode,
)


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


def _smoke_settings() -> SimpleNamespace:
    backend_dir = Path(__file__).resolve().parents[1]
    project_root = backend_dir.parent
    runtime_cache_dir = backend_dir / "runtime_cache"
    return SimpleNamespace(
        project_root=project_root,
        runtime_cache_dir=runtime_cache_dir,
        inference_backend=os.getenv("APP_INFERENCE_BACKEND", "bandon_mps"),
        inference_tiled_mode_auto=_bool_env("APP_INFERENCE_TILED_MODE_AUTO", True),
        inference_tile_size=_int_env("APP_INFERENCE_TILE_SIZE", 1024),
        inference_tile_overlap=_int_env("APP_INFERENCE_TILE_OVERLAP", 128),
        inference_tile_batch_size=_int_env("APP_INFERENCE_TILE_BATCH_SIZE", 1),
        inference_max_in_memory_pixels=_int_env("APP_INFERENCE_MAX_IN_MEMORY_PIXELS", 25_000_000),
        inference_heavy_batch_tile_threshold=_int_env("APP_INFERENCE_HEAVY_BATCH_TILE_THRESHOLD", 2000),
        default_change_threshold=_float_env("APP_CHANGE_THRESHOLD", 0.35),
        bandon_repo_dir=project_root / "vendor" / "BANDON-mps",
        bandon_env_prefix=project_root / "vendor" / "BANDON-mps" / ".conda-macos-mps",
        bandon_config_path=project_root / "vendor" / "BANDON-mps" / "workdirs_bandon" / "MTGCDNet" / "config.py",
        bandon_checkpoint_path=project_root / "vendor" / "BANDON-mps" / "checkpoints" / "mtgcdnet_iter_40000.pth",
        bandon_device=os.getenv("APP_BANDON_DEVICE", "mps"),
        bandon_allow_mps_fallback=_bool_env("APP_BANDON_ALLOW_MPS_FALLBACK", False),
        bandon_skip_invalid_crops=_bool_env("APP_BANDON_SKIP_INVALID_CROPS", True),
        bandon_skip_outside_aoi_crops=_bool_env("APP_BANDON_SKIP_OUTSIDE_AOI_CROPS", True),
        bandon_skip_nodata_crops=_bool_env("APP_BANDON_SKIP_NODATA_CROPS", True),
        bandon_min_valid_ratio_within_aoi=_float_env("APP_BANDON_MIN_VALID_RATIO_WITHIN_AOI", 0.01),
    )


def _histogram(path: Path) -> dict[str, object]:
    with rasterio.open(path) as src:
        values = src.read(1, masked=True)
        unique, counts = np.unique(values.compressed(), return_counts=True)
        return {
            "width": src.width,
            "height": src.height,
            "dtype": src.dtypes[0],
            "crs": str(src.crs),
            "bounds": [float(src.bounds.left), float(src.bounds.bottom), float(src.bounds.right), float(src.bounds.top)],
            "histogram": {str(int(k)): int(v) for k, v in zip(unique.tolist(), counts.tolist(), strict=False)},
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded tiled inference smoke check on existing mosaics.")
    parser.add_argument("--t1-cache-dir", required=True, type=Path)
    parser.add_argument("--t2-cache-dir", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("runtime_cache/tiled_inference_smoke"))
    parser.add_argument("--run-id", default="tiled-smoke")
    parser.add_argument("--max-tiles", type=int, default=4)
    parser.add_argument("--predictor", choices=("difference", "synthetic", "bandon"), default="difference")
    args = parser.parse_args()

    settings = _smoke_settings()
    t1_mosaic = args.t1_cache_dir / "mosaic.tif"
    t2_mosaic = args.t2_cache_dir / "mosaic.tif"
    t1_valid = args.t1_cache_dir / "valid_mask.tif"
    t2_valid = args.t2_cache_dir / "valid_mask.tif"
    tile_count = 0
    metadata_path = args.t2_cache_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            tile_count = int(metadata.get("selected_tile_count") or metadata.get("tile_count") or 0)
        except Exception:
            tile_count = 0
    with rasterio.open(t2_mosaic) as src:
        decision = select_inference_mode(width=src.width, height=src.height, tile_count=tile_count, settings=settings)
    if args.predictor == "bandon":
        predictor = make_bandon_patch_predictor(
            settings=settings,
            effective_backend=settings.inference_backend,
            threshold=settings.default_change_threshold,
        )
    elif args.predictor == "synthetic":
        predictor = make_synthetic_square_patch_predictor(every_n_tiles=3)
    else:
        predictor = make_difference_patch_predictor(threshold=0.08)
    result = run_tiled_inference(
        t1_mosaic_path=t1_mosaic,
        t2_mosaic_path=t2_mosaic,
        t1_valid_mask_path=t1_valid,
        t2_valid_mask_path=t2_valid,
        output_dir=args.out_dir,
        run_id=args.run_id,
        settings=settings,
        config=TiledInferenceConfig.from_settings(settings, threshold=settings.default_change_threshold),
        predictor=predictor,
        release_t1="WB_2020_R04",
        release_t2="WB_2026_R04",
        max_tiles=args.max_tiles,
    )
    summary = {
        "decision": decision.__dict__,
        "result": {
            "probability_path": str(result.probability_path),
            "mask_path": str(result.mask_path),
            "geojsonl_path": str(result.geojsonl_path),
            "metadata_path": str(result.metadata_path),
            "state_path": str(result.state_path),
            "processed_tiles": result.processed_tiles,
            "skipped_tiles": result.skipped_tiles,
            "total_tiles": result.total_tiles,
            "feature_count": result.feature_count,
            "probability_stats": result.probability_stats,
        },
        "mask": _histogram(result.mask_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
