#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window, transform as window_transform
from shapely.geometry import shape


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import Settings  # noqa: E402
from src.domain.tiled_inference import (  # noqa: E402
    TiledInferenceConfig,
    make_bandon_patch_predictor,
    run_tiled_inference,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark cli_per_tile vs persistent_runner BANDON tiled inference.")
    parser.add_argument("--t1-mosaic", required=True, type=Path)
    parser.add_argument("--t2-mosaic", required=True, type=Path)
    parser.add_argument("--t1-valid-mask", required=True, type=Path)
    parser.add_argument("--t2-valid-mask", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--crop-width", type=int, default=4096)
    parser.add_argument("--crop-height", type=int, default=3328)
    parser.add_argument("--tile-size", type=int, default=1024)
    parser.add_argument("--overlap", type=int, default=128)
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda", "mps"], default="auto")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cli-repeats", type=int, default=None)
    parser.add_argument("--persistent-repeats", type=int, default=None)
    parser.add_argument("--modes", choices=["both", "cli_per_tile", "persistent_runner"], default="both")
    parser.add_argument("--allow-existing", action="store_true")
    parser.add_argument("--skip-crop-inputs", action="store_true")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def crop_raster(src_path: Path, dst_path: Path, *, width: int, height: int) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        out_width = min(int(width), int(src.width))
        out_height = min(int(height), int(src.height))
        window = Window(0, 0, out_width, out_height)
        profile = src.profile.copy()
        profile.update(
            width=out_width,
            height=out_height,
            transform=window_transform(window, src.transform),
            compress="deflate",
            tiled=True,
            BIGTIFF="IF_SAFER",
        )
        data = src.read(window=window, boundless=False)
        with rasterio.open(dst_path, "w", **profile) as dst:
            dst.write(data)


def materialize_inputs(args: argparse.Namespace, output_root: Path) -> dict[str, Path]:
    input_dir = output_root / "inputs"
    paths = {
        "t1_mosaic": input_dir / "t1_mosaic_crop.tif",
        "t2_mosaic": input_dir / "t2_mosaic_crop.tif",
        "t1_valid_mask": input_dir / "t1_valid_mask_crop.tif",
        "t2_valid_mask": input_dir / "t2_valid_mask_crop.tif",
    }
    if not args.skip_crop_inputs:
        if all(path.exists() for path in paths.values()):
            return paths
        crop_raster(args.t1_mosaic, paths["t1_mosaic"], width=args.crop_width, height=args.crop_height)
        crop_raster(args.t2_mosaic, paths["t2_mosaic"], width=args.crop_width, height=args.crop_height)
        crop_raster(args.t1_valid_mask, paths["t1_valid_mask"], width=args.crop_width, height=args.crop_height)
        crop_raster(args.t2_valid_mask, paths["t2_valid_mask"], width=args.crop_width, height=args.crop_height)
    return paths


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def timing_value(summary: dict[str, Any], field: str, stat: str, default: float = 0.0) -> float:
    value = ((summary.get("summary") or {}).get(field) or {}).get(stat)
    return float(value) if isinstance(value, (int, float)) else default


def collect_metrics(output_dir: Path, *, wall_time_seconds: float) -> dict[str, Any]:
    metadata = read_json(output_dir / "tiled_inference_metadata.json")
    summary_path = output_dir / "timing_summary.json"
    summary = read_json(summary_path)
    processed_tiles = int(metadata.get("processed_tiles_this_run") or metadata.get("processed_tiles") or 0)
    total_wall = float(metadata.get("duration_seconds") or wall_time_seconds)
    output_read_write_ms_total = (
        timing_value(summary, "bandon_output_read_ms", "total_ms")
        + timing_value(summary, "prediction_geotiff_write_ms", "total_ms")
        + timing_value(summary, "child_output_write_ms", "total_ms")
    )
    tile_total_max = timing_value(summary, "tile_total_wall_ms", "max_ms")
    model_load_count_total = int(round(timing_value(summary, "child_model_load_count_this_prediction", "total_ms")))
    processed_tiles = int(metadata.get("processed_tiles_this_run") or metadata.get("processed_tiles") or 0)
    return {
        "processed_tiles": processed_tiles,
        "total_tiles": int(metadata.get("total_tiles") or 0),
        "selected_tiles": int(metadata.get("selected_tiles") or 0),
        "total_wall_time_seconds": round(total_wall, 3),
        "tiles_per_second": round(processed_tiles / total_wall, 6) if total_wall > 0 else 0.0,
        "seconds_per_tile_mean": round(timing_value(summary, "tile_total_wall_ms", "mean_ms") / 1000.0, 6),
        "seconds_per_tile_median": round(timing_value(summary, "tile_total_wall_ms", "median_ms") / 1000.0, 6),
        "seconds_per_tile_p90": round(timing_value(summary, "tile_total_wall_ms", "p90_ms") / 1000.0, 6),
        "seconds_per_tile_p95": round(timing_value(summary, "tile_total_wall_ms", "p95_ms") / 1000.0, 6),
        "seconds_per_tile_max": round(tile_total_max / 1000.0, 6),
        "model_load_count_total": model_load_count_total,
        "checkpoint_load_count_total": model_load_count_total,
        "model_reused_tile_count": int(round(timing_value(summary, "child_model_reused_numeric", "total_ms"))),
        "model_reused_ratio": round(
            int(round(timing_value(summary, "child_model_reused_numeric", "total_ms"))) / processed_tiles,
            6,
        ) if processed_tiles else 0.0,
        "model_load_ms_total": round(
            timing_value(summary, "child_model_build_ms", "total_ms")
            + timing_value(summary, "child_model_to_device_ms", "total_ms"),
            3,
        ),
        "checkpoint_load_ms_total": round(timing_value(summary, "child_checkpoint_load_ms", "total_ms"), 3),
        "forward_ms_total": round(timing_value(summary, "child_forward_total_ms", "total_ms"), 3),
        "forward_ms_mean": round(timing_value(summary, "child_forward_total_ms", "mean_ms"), 3),
        "subprocess_wall_ms_total": round(timing_value(summary, "bandon_subprocess_wall_ms", "total_ms"), 3),
        "persistent_worker_wall_ms_total": round(timing_value(summary, "bandon_persistent_request_ms", "total_ms"), 3),
        "persistent_request_ms_total": round(timing_value(summary, "bandon_persistent_request_ms", "total_ms"), 3),
        "patch_png_write_ms_total": round(timing_value(summary, "patch_png_write_ms", "total_ms"), 3),
        "bandon_output_read_ms_total": round(timing_value(summary, "bandon_output_read_ms", "total_ms"), 3),
        "prediction_geotiff_write_ms_total": round(timing_value(summary, "prediction_geotiff_write_ms", "total_ms"), 3),
        "vectorization_ms_total": round(timing_value(summary, "vectorization_ms", "total_ms"), 3),
        "state_progress_write_ms_total": round(timing_value(summary, "state_progress_write_ms", "total_ms"), 3),
        "output_read_write_ms_total": round(output_read_write_ms_total, 3),
        "peak_rss_mb": (((metadata.get("rss_mb_samples") or {}).get("max")) or metadata.get("rss_mb")),
        "timing_summary_json_size": summary_path.stat().st_size,
        "timing_summary_path": str(summary_path),
        "metadata_path": str(output_dir / "tiled_inference_metadata.json"),
        "probability_path": str(output_dir / "prediction_change_probability.tif"),
        "mask_path": str(output_dir / "prediction_change_mask.tif"),
        "geojsonl_path": str(output_dir / "prediction_change_polygons.geojsonl"),
    }


def run_mode(
    *,
    mode: str,
    repeat_index: int,
    args: argparse.Namespace,
    inputs: dict[str, Path],
    output_root: Path,
) -> dict[str, Any]:
    run_id = f"bench_{mode}_{repeat_index}_{utc_stamp()}"
    output_dir = output_root / mode / f"run_{repeat_index:02d}"
    runtime_dir = output_root / "runtime" / mode / f"run_{repeat_index:02d}"
    settings = Settings(
        runtime_cache_dir=runtime_dir,
        inference_timing_enabled=True,
        bandon_inference_mode=mode,  # type: ignore[arg-type]
        bandon_device=args.device,
    )
    config = TiledInferenceConfig(
        tile_size=args.tile_size,
        overlap=args.overlap,
        batch_size=1,
        threshold=args.threshold,
        max_in_memory_pixels=settings.inference_max_in_memory_pixels,
        heavy_batch_tile_threshold=settings.inference_heavy_batch_tile_threshold,
    )
    predictor = make_bandon_patch_predictor(
        settings=settings,
        effective_backend=settings.inference_backend,
        threshold=args.threshold,
    )
    parent_rss_samples: list[dict[str, Any]] = []

    def _record_progress(payload: dict[str, object]) -> None:
        rss_mb = payload.get("rss_mb")
        processed = payload.get("processed_tiles")
        if isinstance(rss_mb, (int, float)) and isinstance(processed, (int, float)):
            parent_rss_samples.append(
                {
                    "processed_tiles": int(processed),
                    "rss_mb": float(rss_mb),
                    "tile_index": payload.get("current_tile_index"),
                    "mode": mode,
                    "repeat_index": repeat_index,
                }
            )

    started = time.monotonic()
    run_tiled_inference(
        t1_mosaic_path=inputs["t1_mosaic"],
        t2_mosaic_path=inputs["t2_mosaic"],
        t1_valid_mask_path=inputs["t1_valid_mask"],
        t2_valid_mask_path=inputs["t2_valid_mask"],
        output_dir=output_dir,
        run_id=run_id,
        settings=settings,
        config=config,
        predictor=predictor,
        release_t1="benchmark_t1",
        release_t2="benchmark_t2",
        progress_callback=_record_progress,
    )
    wall = time.monotonic() - started
    metrics = collect_metrics(output_dir, wall_time_seconds=wall)
    metrics.update(
        {
            "mode": mode,
            "repeat_index": repeat_index,
            "output_dir": str(output_dir),
            "run_id": run_id,
            "parent_rss_samples": parent_rss_samples,
            "worker_crash": False,
        }
    )
    return metrics


def polygon_metrics(path: Path) -> dict[str, Any]:
    count = 0
    total_area = 0.0
    bounds: list[float] | None = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            feature = json.loads(line)
            count += 1
            props = feature.get("properties") or {}
            if isinstance(props.get("area_m2"), (int, float)):
                total_area += float(props["area_m2"])
            geom = shape(feature["geometry"])
            gbounds = list(geom.bounds)
            if bounds is None:
                bounds = gbounds
            else:
                bounds = [
                    min(bounds[0], gbounds[0]),
                    min(bounds[1], gbounds[1]),
                    max(bounds[2], gbounds[2]),
                    max(bounds[3], gbounds[3]),
                ]
    return {"count": count, "total_area_m2": total_area, "bounds": bounds}


def compare_outputs(old_metrics: dict[str, Any], new_metrics: dict[str, Any]) -> dict[str, Any]:
    old_prob_path = Path(old_metrics["probability_path"])
    new_prob_path = Path(new_metrics["probability_path"])
    with rasterio.open(old_prob_path) as old_src, rasterio.open(new_prob_path) as new_src:
        old_prob = old_src.read(1)
        new_prob = new_src.read(1)
        raster_metadata_equal = {
            "shape": (old_src.width, old_src.height) == (new_src.width, new_src.height),
            "crs": str(old_src.crs) == str(new_src.crs),
            "transform": old_src.transform == new_src.transform,
            "bounds": tuple(old_src.bounds) == tuple(new_src.bounds),
            "dtype": old_src.dtypes[0] == new_src.dtypes[0],
            "nodata": old_src.nodata == new_src.nodata,
        }
    diff = np.abs(old_prob.astype(np.float32) - new_prob.astype(np.float32))
    finite = diff[np.isfinite(diff)]

    with rasterio.open(old_metrics["mask_path"]) as old_mask_src, rasterio.open(new_metrics["mask_path"]) as new_mask_src:
        old_mask = old_mask_src.read(1)
        new_mask = new_mask_src.read(1)
        mask_mismatch_count = int(np.count_nonzero(old_mask != new_mask))
        mask_pixel_count = int(old_mask.size)

    old_poly = polygon_metrics(Path(old_metrics["geojsonl_path"]))
    new_poly = polygon_metrics(Path(new_metrics["geojsonl_path"]))
    return {
        "probability_raster_metadata_equal": raster_metadata_equal,
        "probability_shape_match": raster_metadata_equal["shape"],
        "probability_crs_match": raster_metadata_equal["crs"],
        "probability_transform_match": raster_metadata_equal["transform"],
        "probability_bounds_match": raster_metadata_equal["bounds"],
        "probability_dtype_match": raster_metadata_equal["dtype"],
        "probability_nodata_match": raster_metadata_equal["nodata"],
        "valid_pixel_count": int(finite.size),
        "max_abs_diff": float(np.max(finite)) if finite.size else 0.0,
        "mean_abs_diff": float(np.mean(finite)) if finite.size else 0.0,
        "p99_abs_diff": float(np.percentile(finite, 99)) if finite.size else 0.0,
        "binary_mask_mismatch_count": mask_mismatch_count,
        "binary_mask_mismatch_ratio": (mask_mismatch_count / mask_pixel_count) if mask_pixel_count else 0.0,
        "polygon_count_old": old_poly["count"],
        "polygon_count_new": new_poly["count"],
        "old_polygon_count": old_poly["count"],
        "new_polygon_count": new_poly["count"],
        "polygon_count_delta": int(new_poly["count"] - old_poly["count"]),
        "old_polygon_total_area_m2": old_poly["total_area_m2"],
        "new_polygon_total_area_m2": new_poly["total_area_m2"],
        "polygon_total_area_delta_m2": new_poly["total_area_m2"] - old_poly["total_area_m2"],
        "old_polygon_bounds": old_poly["bounds"],
        "new_polygon_bounds": new_poly["bounds"],
    }


def copy_representative_artifacts(output_root: Path, old_metrics: dict[str, Any], new_metrics: dict[str, Any]) -> None:
    shutil.copy2(old_metrics["timing_summary_path"], output_root / "cli_per_tile_timing_summary.json")
    shutil.copy2(new_metrics["timing_summary_path"], output_root / "persistent_runner_timing_summary.json")
    for label, metrics in (("cli", old_metrics), ("persistent", new_metrics)):
        output_dir = Path(metrics["output_dir"])
        metadata_files = _tile_metadata_paths(output_dir)
        if metadata_files:
            shutil.copy2(metadata_files[0], output_root / f"representative_{label}_tile_metadata.json")


def aggregate_runs(runs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    selected = [item for item in runs if item["mode"] == mode]
    numeric_keys = [
        "processed_tiles",
        "total_wall_time_seconds",
        "tiles_per_second",
        "seconds_per_tile_mean",
        "seconds_per_tile_median",
        "seconds_per_tile_p90",
        "seconds_per_tile_p95",
        "seconds_per_tile_max",
        "model_load_count_total",
        "checkpoint_load_count_total",
        "model_reused_tile_count",
        "model_reused_ratio",
        "model_load_ms_total",
        "checkpoint_load_ms_total",
        "forward_ms_total",
        "forward_ms_mean",
        "subprocess_wall_ms_total",
        "persistent_worker_wall_ms_total",
        "persistent_request_ms_total",
        "patch_png_write_ms_total",
        "bandon_output_read_ms_total",
        "prediction_geotiff_write_ms_total",
        "vectorization_ms_total",
        "state_progress_write_ms_total",
        "output_read_write_ms_total",
        "peak_rss_mb",
    ]
    aggregate: dict[str, Any] = {"mode": mode, "repeat_count": len(selected)}
    for key in numeric_keys:
        values = [float(item[key]) for item in selected if isinstance(item.get(key), (int, float))]
        if values:
            aggregate[key] = round(float(np.mean(values)), 6)
            aggregate[f"{key}_min"] = round(float(np.min(values)), 6)
            aggregate[f"{key}_max"] = round(float(np.max(values)), 6)
    return aggregate


def _tile_metadata_paths(output_dir: Path) -> list[Path]:
    return sorted((output_dir / "tiles").glob("*/bandon_run/run_metadata.json"))


def _worker_rss_samples_for_run(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    output_dir = Path(metrics["output_dir"])
    for path in _tile_metadata_paths(output_dir):
        try:
            metadata = read_json(path)
        except Exception:
            continue
        parent_timing = metadata.get("parent_timing_ms") if isinstance(metadata.get("parent_timing_ms"), dict) else {}
        runner_timing = metadata.get("bandon_runner_timing_ms") if isinstance(metadata.get("bandon_runner_timing_ms"), dict) else {}
        rss = parent_timing.get("persistent_worker_rss_mb")
        if not isinstance(rss, (int, float)):
            rss = runner_timing.get("persistent_worker_rss_mb")
        tile_id = metadata.get("tile_id")
        if isinstance(rss, (int, float)) and isinstance(tile_id, (int, float)):
            samples.append(
                {
                    "tile_id": int(tile_id),
                    "processed_tile_ordinal": len(samples) + 1,
                    "rss_mb": float(rss),
                    "worker_pid": runner_timing.get("persistent_worker_pid"),
                    "mode": metrics.get("mode"),
                    "repeat_index": metrics.get("repeat_index"),
                }
            )
    return samples


def _rss_slope_mb_per_100_tiles(samples: list[dict[str, Any]]) -> float | None:
    if len(samples) < 2:
        return None
    xs = np.array([float(item["processed_tile_ordinal"]) for item in samples], dtype=np.float64)
    ys = np.array([float(item["rss_mb"]) for item in samples], dtype=np.float64)
    if np.all(xs == xs[0]):
        return None
    slope_per_tile = float(np.polyfit(xs, ys, 1)[0])
    return round(slope_per_tile * 100.0, 6)


def _memory_growth_detected(samples: list[dict[str, Any]]) -> bool:
    if len(samples) < 20:
        return False
    warmup_count = min(50, max(10, len(samples) // 5))
    steady = samples[warmup_count:]
    if len(steady) < 10:
        return False
    values = [float(item["rss_mb"]) for item in steady]
    rss_delta = values[-1] - values[0]
    rss_range = max(values) - min(values)
    increases = sum(1 for before, after in zip(values, values[1:]) if after >= before)
    increasing_ratio = increases / max(len(values) - 1, 1)
    slope = _rss_slope_mb_per_100_tiles(steady) or 0.0
    return bool((rss_delta > 50.0 and increasing_ratio >= 0.80) or (slope > 50.0 and rss_range > 50.0))


def build_memory_profile(runs: list[dict[str, Any]]) -> dict[str, Any]:
    run_profiles: list[dict[str, Any]] = []
    all_growth = False
    for metrics in runs:
        if metrics.get("mode") != "persistent_runner":
            continue
        samples = _worker_rss_samples_for_run(metrics)
        values = [float(item["rss_mb"]) for item in samples]
        slope = _rss_slope_mb_per_100_tiles(samples)
        growth = _memory_growth_detected(samples)
        all_growth = all_growth or growth
        run_profiles.append(
            {
                "mode": metrics.get("mode"),
                "repeat_index": metrics.get("repeat_index"),
                "sample_count": len(samples),
                "rss_start_mb": values[0] if values else None,
                "rss_end_mb": values[-1] if values else None,
                "rss_peak_mb": max(values) if values else None,
                "rss_min_mb": min(values) if values else None,
                "rss_max_minus_min_mb": (max(values) - min(values)) if values else None,
                "rss_slope_mb_per_100_tiles": slope,
                "memory_growth_detected": growth,
                "worker_crash": bool(metrics.get("worker_crash")),
                "samples": samples,
            }
        )
    first_values = [float(item["rss_mb"]) for profile in run_profiles for item in profile.get("samples", [])]
    return {
        "persistent_runner_repeat_count": len(run_profiles),
        "rss_start_mb": first_values[0] if first_values else None,
        "rss_end_mb": first_values[-1] if first_values else None,
        "rss_peak_mb": max(first_values) if first_values else None,
        "rss_min_mb": min(first_values) if first_values else None,
        "rss_max_minus_min_mb": (max(first_values) - min(first_values)) if first_values else None,
        "rss_slope_mb_per_100_tiles": _rss_slope_mb_per_100_tiles(
            [
                {"processed_tile_ordinal": index + 1, "rss_mb": value}
                for index, value in enumerate(first_values)
            ]
        ),
        "memory_growth_detected": all_growth,
        "swap_or_memory_pressure_observation_if_available": None,
        "runs": run_profiles,
    }


def write_benchmark_plan(path: Path, *, args: argparse.Namespace, output_root: Path) -> None:
    cli_command = (
        "APP_INFERENCE_TIMING_ENABLED=true APP_BANDON_INFERENCE_MODE=cli_per_tile "
        "backend/.venv/bin/python scripts/benchmark_bandon_inference_modes.py "
        f"--modes cli_per_tile --output-root {output_root} --allow-existing "
        f"--t1-mosaic {args.t1_mosaic} --t2-mosaic {args.t2_mosaic} "
        f"--t1-valid-mask {args.t1_valid_mask} --t2-valid-mask {args.t2_valid_mask} "
        f"--crop-width {args.crop_width} --crop-height {args.crop_height} "
        f"--tile-size {args.tile_size} --overlap {args.overlap} --threshold {args.threshold} "
        f"--device {args.device} --cli-repeats {args.cli_repeats or args.repeats}"
    )
    persistent_command = (
        "APP_INFERENCE_TIMING_ENABLED=true APP_BANDON_INFERENCE_MODE=persistent_runner "
        "backend/.venv/bin/python scripts/benchmark_bandon_inference_modes.py "
        f"--modes persistent_runner --output-root {output_root} --allow-existing --skip-crop-inputs "
        f"--t1-mosaic {args.t1_mosaic} --t2-mosaic {args.t2_mosaic} "
        f"--t1-valid-mask {args.t1_valid_mask} --t2-valid-mask {args.t2_valid_mask} "
        f"--crop-width {args.crop_width} --crop-height {args.crop_height} "
        f"--tile-size {args.tile_size} --overlap {args.overlap} --threshold {args.threshold} "
        f"--device {args.device} --persistent-repeats {args.persistent_repeats or args.repeats}"
    )
    path.write_text(
        "\n".join(
            [
                "# Medium Persistent Runner Benchmark Plan",
                "",
                f"- Output root: `{output_root}`",
                f"- Tile size: `{args.tile_size}`",
                f"- Overlap: `{args.overlap}`",
                f"- Threshold: `{args.threshold}`",
                f"- Device: `{args.device}`",
                f"- Crop: `{args.crop_width}x{args.crop_height}`",
                "",
                "## Runnable Commands",
                "",
                "```bash",
                cli_command,
                persistent_command,
                "```",
                "",
                "The script uses real tiled inference code paths and the real configured BANDON checkpoint.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_acceptance_decision(
    path: Path,
    *,
    old_metrics: dict[str, Any],
    new_metrics: dict[str, Any],
    comparison: dict[str, Any],
    equivalence: dict[str, Any],
    memory_profile: dict[str, Any],
) -> None:
    criteria = {
        "persistent_runner benchmark used 200-500 real tiles": 200 <= int(new_metrics.get("processed_tiles") or 0) <= 500,
        "speedup_total_wall >= 5.0 OR speedup_tiles_per_second >= 5.0": bool(
            (comparison.get("speedup_total_wall") or 0) >= 5.0
            or (comparison.get("speedup_tiles_per_second") or 0) >= 5.0
        ),
        "persistent_runner model_load_count_total <= 1": float(new_metrics.get("model_load_count_total") or 0) <= 1.0,
        "max_abs_diff == 0.0": float(equivalence.get("max_abs_diff") or 0.0) == 0.0,
        "binary_mask_mismatch_count == 0": int(equivalence.get("binary_mask_mismatch_count") or 0) == 0,
        "memory_growth_detected == false": memory_profile.get("memory_growth_detected") is False,
        "worker_crash == false": not any(run.get("worker_crash") for run in memory_profile.get("runs", [])),
    }
    decision = "PASS" if all(criteria.values()) else "FAIL"
    lines = ["# Acceptance Decision", "", f"Decision: **{decision}**", "", "| criterion | result |", "|---|---:|"]
    for label, passed in criteria.items():
        lines.append(f"| {label} | {'PASS' if passed else 'FAIL'} |")
    lines.extend(
        [
            "",
            f"- CLI tiles: `{old_metrics.get('processed_tiles')}`",
            f"- Persistent tiles: `{new_metrics.get('processed_tiles')}`",
            f"- Wall speedup: `{comparison.get('speedup_total_wall')}`",
            f"- Tiles/sec speedup: `{comparison.get('speedup_tiles_per_second')}`",
            f"- Persistent model loads: `{new_metrics.get('model_load_count_total')}`",
            f"- Max abs diff: `{equivalence.get('max_abs_diff')}`",
            f"- Binary mask mismatches: `{equivalence.get('binary_mask_mismatch_count')}`",
            f"- Memory growth detected: `{memory_profile.get('memory_growth_detected')}`",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_markdown_report(
    path: Path,
    *,
    old_metrics: dict[str, Any],
    new_metrics: dict[str, Any],
    comparison: dict[str, Any],
    equivalence: dict[str, Any],
    memory_profile: dict[str, Any],
) -> None:
    lines = [
        "# BANDON Inference Mode Benchmark",
        "",
        "| metric | cli_per_tile | persistent_runner |",
        "|---|---:|---:|",
    ]
    for key in (
        "processed_tiles",
        "total_wall_time_seconds",
        "tiles_per_second",
        "seconds_per_tile_mean",
        "seconds_per_tile_median",
        "seconds_per_tile_p90",
        "seconds_per_tile_p95",
        "seconds_per_tile_max",
        "model_load_count_total",
        "checkpoint_load_count_total",
        "checkpoint_load_ms_total",
        "forward_ms_total",
        "subprocess_wall_ms_total",
        "persistent_worker_wall_ms_total",
        "peak_rss_mb",
    ):
        lines.append(f"| {key} | {old_metrics.get(key)} | {new_metrics.get(key)} |")
    lines.extend(
        [
            "",
            "## Speedup",
            "",
            f"- total wall speedup: {comparison['speedup_total_wall']}",
            f"- tiles/sec speedup: {comparison['speedup_tiles_per_second']}",
            f"- model load reduction: {comparison['model_load_reduction']}",
            f"- latency reduction percent: {comparison['per_tile_latency_reduction_percent']}",
            "",
            "## Memory Stability",
            "",
            f"- rss_start_mb: {memory_profile.get('rss_start_mb')}",
            f"- rss_end_mb: {memory_profile.get('rss_end_mb')}",
            f"- rss_peak_mb: {memory_profile.get('rss_peak_mb')}",
            f"- rss_max_minus_min_mb: {memory_profile.get('rss_max_minus_min_mb')}",
            f"- rss_slope_mb_per_100_tiles: {memory_profile.get('rss_slope_mb_per_100_tiles')}",
            f"- memory_growth_detected: {memory_profile.get('memory_growth_detected')}",
            "",
            "## Output Equivalence",
            "",
            f"- max_abs_diff: {equivalence['max_abs_diff']}",
            f"- mean_abs_diff: {equivalence['mean_abs_diff']}",
            f"- p99_abs_diff: {equivalence['p99_abs_diff']}",
            f"- binary_mask_mismatch_count: {equivalence['binary_mask_mismatch_count']}",
            f"- polygon_count_delta: {equivalence['polygon_count_delta']}",
            f"- polygon_total_area_delta_m2: {equivalence['polygon_total_area_delta_m2']}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_root = args.output_root or (REPO_ROOT / "artifacts" / "benchmarks" / f"bandon_persistent_runner_{utc_stamp()}")
    output_root.mkdir(parents=True, exist_ok=args.allow_existing)
    write_benchmark_plan(output_root / "benchmark_plan.md", args=args, output_root=output_root)
    inputs = materialize_inputs(args, output_root)

    runs_path = output_root / "runs_partial.json"
    runs: list[dict[str, Any]] = []
    if args.allow_existing and runs_path.exists():
        loaded = read_json(runs_path).get("runs")
        if isinstance(loaded, list):
            runs = [item for item in loaded if isinstance(item, dict)]

    cli_repeats = args.cli_repeats if args.cli_repeats is not None else args.repeats
    persistent_repeats = args.persistent_repeats if args.persistent_repeats is not None else args.repeats
    requested_modes: list[tuple[str, int]] = []
    if args.modes in {"both", "cli_per_tile"}:
        requested_modes.extend(("cli_per_tile", index) for index in range(1, cli_repeats + 1))
    if args.modes in {"both", "persistent_runner"}:
        requested_modes.extend(("persistent_runner", index) for index in range(1, persistent_repeats + 1))

    existing_keys = {(item.get("mode"), item.get("repeat_index")) for item in runs}
    for mode, repeat_index in requested_modes:
        if (mode, repeat_index) in existing_keys:
            continue
        runs.append(run_mode(mode=mode, repeat_index=repeat_index, args=args, inputs=inputs, output_root=output_root))
        runs_path.write_text(json.dumps({"runs": runs}, indent=2, sort_keys=True), encoding="utf-8")

    if not any(item.get("mode") == "cli_per_tile" for item in runs) or not any(item.get("mode") == "persistent_runner" for item in runs):
        print(json.dumps({"output_root": str(output_root), "runs": runs, "finalized": False}, indent=2))
        return 0

    old_first = next(item for item in runs if item["mode"] == "cli_per_tile")
    new_first = next(item for item in runs if item["mode"] == "persistent_runner")
    old_metrics = aggregate_runs(runs, "cli_per_tile")
    new_metrics = aggregate_runs(runs, "persistent_runner")
    equivalence = compare_outputs(old_first, new_first)
    memory_profile = build_memory_profile(runs)
    comparison = {
        "speedup_total_wall": (
            round(old_metrics["total_wall_time_seconds"] / new_metrics["total_wall_time_seconds"], 6)
            if new_metrics["total_wall_time_seconds"]
            else None
        ),
        "speedup_tiles_per_second": (
            round(new_metrics["tiles_per_second"] / old_metrics["tiles_per_second"], 6)
            if old_metrics["tiles_per_second"]
            else None
        ),
        "model_load_reduction": (
            round(old_metrics["model_load_count_total"] / new_metrics["model_load_count_total"], 6)
            if new_metrics["model_load_count_total"]
            else None
        ),
        "per_tile_latency_reduction_percent": (
            round(
                100.0
                * (old_metrics["seconds_per_tile_mean"] - new_metrics["seconds_per_tile_mean"])
                / old_metrics["seconds_per_tile_mean"],
                3,
            )
            if old_metrics["seconds_per_tile_mean"]
            else None
        ),
    }
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_paths": {key: str(value) for key, value in inputs.items()},
        "crop_width": args.crop_width,
        "crop_height": args.crop_height,
        "tile_size": args.tile_size,
        "overlap": args.overlap,
        "threshold": args.threshold,
        "device": args.device,
        "repeats": args.repeats,
        "runs": runs,
        "baseline_cli_per_tile": old_metrics,
        "new_persistent_runner": new_metrics,
        "speedup": comparison,
        "output_equivalence": equivalence,
        "memory_profile": memory_profile,
    }
    (output_root / "benchmark_comparison.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    (output_root / "output_equivalence_report.json").write_text(json.dumps(equivalence, indent=2, sort_keys=True), encoding="utf-8")
    (output_root / "memory_profile_persistent_runner.json").write_text(
        json.dumps(memory_profile, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    copy_representative_artifacts(output_root, old_first, new_first)
    write_markdown_report(
        output_root / "benchmark_comparison.md",
        old_metrics=old_metrics,
        new_metrics=new_metrics,
        comparison=comparison,
        equivalence=equivalence,
        memory_profile=memory_profile,
    )
    write_acceptance_decision(
        output_root / "acceptance_decision.md",
        old_metrics=old_metrics,
        new_metrics=new_metrics,
        comparison=comparison,
        equivalence=equivalence,
        memory_profile=memory_profile,
    )
    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "speedup": comparison,
                "equivalence": equivalence,
                "memory_profile": {
                    key: value for key, value in memory_profile.items() if key != "runs"
                },
                "finalized": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
