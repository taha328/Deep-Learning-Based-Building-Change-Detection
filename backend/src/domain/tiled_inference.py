from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import resource
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

import numpy as np
from PIL import Image
import rasterio
from rasterio.features import geometry_mask, shapes
from rasterio.transform import Affine
from rasterio.windows import Window, transform as window_transform
from pyproj import Geod
from shapely.geometry import mapping, shape

from src.domain.change_products import threshold_change_probability
from src.domain.inference_timing import elapsed_ms, safe_merge_json_file, write_timing_summary
from src.utils.geometry import parse_aoi_geometry, reproject_geometry

if TYPE_CHECKING:
    from src.config import Settings


LOGGER = logging.getLogger(__name__)
GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class InferenceModeDecision:
    mode: str
    reason: str
    width: int
    height: int
    pixel_count: int
    estimated_in_memory_pixels: int
    tile_count: int
    max_in_memory_pixels: int
    heavy_batch_tile_threshold: int


@dataclass(frozen=True)
class TiledInferenceConfig:
    tile_size: int
    overlap: int
    batch_size: int
    threshold: float
    max_in_memory_pixels: int
    heavy_batch_tile_threshold: int

    @classmethod
    def from_settings(cls, settings: Settings, *, threshold: float) -> "TiledInferenceConfig":
        return cls(
            tile_size=settings.inference_tile_size,
            overlap=settings.inference_tile_overlap,
            batch_size=settings.inference_tile_batch_size,
            threshold=threshold,
            max_in_memory_pixels=settings.inference_max_in_memory_pixels,
            heavy_batch_tile_threshold=settings.inference_heavy_batch_tile_threshold,
        )

    @property
    def stride(self) -> int:
        return self.tile_size - (2 * self.overlap)


@dataclass(frozen=True)
class InferenceTile:
    index: int
    window: Window


@dataclass(frozen=True)
class PatchPrediction:
    probability: np.ndarray
    mask: np.ndarray
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TiledInferenceResult:
    probability_path: Path
    mask_path: Path
    geojsonl_path: Path
    metadata_path: Path
    state_path: Path
    feature_count: int
    processed_tiles: int
    skipped_tiles: int
    total_tiles: int
    probability_stats: dict[str, float | None]
    metadata: dict[str, Any]


class PatchPredictor(Protocol):
    def __call__(
        self,
        *,
        tile: InferenceTile,
        t1_rgb: np.ndarray,
        t2_rgb: np.ndarray,
        t1_valid_mask: np.ndarray | None,
        t2_valid_mask: np.ndarray | None,
        aoi_mask: np.ndarray | None,
        work_dir: Path,
    ) -> PatchPrediction:
        ...


ProgressCallback = Callable[[dict[str, object]], None]

TIMING_SUMMARY_FIELDS = [
    "tile_total_wall_ms",
    "raster_window_read_ms",
    "patch_png_write_ms",
    "bandon_subprocess_wall_ms",
    "bandon_persistent_request_ms",
    "bandon_persistent_startup_wall_ms",
    "persistent_worker_rss_mb",
    "child_total_wall_ms",
    "child_model_load_count_this_prediction",
    "child_model_load_count_total",
    "child_model_reused_numeric",
    "child_model_build_ms",
    "child_checkpoint_load_ms",
    "child_model_to_device_ms",
    "child_input_preprocess_ms",
    "child_forward_total_ms",
    "child_output_write_ms",
    "prediction_geotiff_write_ms",
    "vectorization_ms",
    "state_progress_write_ms",
]


def _rss_mb() -> float | None:
    try:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None
    # macOS reports bytes, Linux reports KiB.
    return float(rss / (1024 * 1024) if rss > 10_000_000 else rss / 1024)


def select_inference_mode(
    *,
    width: int,
    height: int,
    tile_count: int,
    settings: Settings,
) -> InferenceModeDecision:
    pixel_count = int(width) * int(height)
    estimated_in_memory_pixels = pixel_count * 2
    forced_by_pixels = estimated_in_memory_pixels > settings.inference_max_in_memory_pixels
    forced_by_tiles = tile_count >= settings.inference_heavy_batch_tile_threshold
    auto_enabled = settings.inference_tiled_mode_auto
    if auto_enabled and forced_by_pixels:
        mode = "tiled"
        reason = "memory_guard"
    elif auto_enabled and forced_by_tiles:
        mode = "tiled"
        reason = "heavy_tile_batch"
    else:
        mode = "direct"
        reason = "within_direct_limits"
    decision = InferenceModeDecision(
        mode=mode,
        reason=reason,
        width=int(width),
        height=int(height),
        pixel_count=pixel_count,
        estimated_in_memory_pixels=estimated_in_memory_pixels,
        tile_count=int(tile_count),
        max_in_memory_pixels=int(settings.inference_max_in_memory_pixels),
        heavy_batch_tile_threshold=int(settings.inference_heavy_batch_tile_threshold),
    )
    LOGGER.info(
        "INFERENCE_MEMORY_GUARD width=%s height=%s pixelCount=%s estimatedInMemoryPixels=%s maxInMemoryPixels=%s tileCount=%s threshold=%s",
        decision.width,
        decision.height,
        decision.pixel_count,
        decision.estimated_in_memory_pixels,
        decision.max_in_memory_pixels,
        decision.tile_count,
        decision.heavy_batch_tile_threshold,
    )
    LOGGER.info(
        "INFERENCE_MODE_SELECTED mode=%s reason=%s width=%s height=%s tileCount=%s",
        decision.mode,
        decision.reason,
        decision.width,
        decision.height,
        decision.tile_count,
    )
    return decision


def iter_inference_tiles(width: int, height: int, *, tile_size: int, overlap: int) -> list[InferenceTile]:
    if overlap * 2 >= tile_size:
        raise ValueError("overlap must be less than half of tile_size")
    stride = tile_size - (2 * overlap)
    def _offsets(size: int) -> list[int]:
        if size <= tile_size:
            return [0]
        offsets: list[int] = []
        value = 0
        while value + tile_size < size:
            offsets.append(value)
            value += stride
        last = max(size - tile_size, 0)
        if not offsets or offsets[-1] != last:
            offsets.append(last)
        return offsets

    tiles: list[InferenceTile] = []
    index = 0
    for y in _offsets(height):
        read_height = min(tile_size, height - y)
        for x in _offsets(width):
            read_width = min(tile_size, width - x)
            tiles.append(InferenceTile(index=index, window=Window(x, y, read_width, read_height)))
            index += 1
    return tiles


def _read_rgb_window(dataset: rasterio.io.DatasetReader, window: Window) -> np.ndarray:
    array = dataset.read(indexes=(1, 2, 3), window=window, boundless=False)
    return np.moveaxis(array, 0, -1).astype(np.uint8, copy=False)


def _read_mask_window(path: Path | None, window: Window) -> np.ndarray | None:
    if path is None or not path.exists():
        return None
    with rasterio.open(path) as src:
        return src.read(1, window=window, boundless=False) > 0


def _aoi_mask_for_window(
    *,
    aoi_geojson: dict[str, Any] | None,
    raster_crs: Any,
    out_shape: tuple[int, int],
    transform: Affine,
) -> np.ndarray | None:
    if aoi_geojson is None or raster_crs is None:
        return None
    aoi_geometry = parse_aoi_geometry(aoi_geojson)
    native = reproject_geometry(aoi_geometry, "EPSG:4326", str(raster_crs))
    return geometry_mask([mapping(native)], out_shape=out_shape, transform=transform, invert=True)


def _write_mask_png(path: Path, mask: np.ndarray) -> Path:
    Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(path)
    return path


def make_bandon_patch_predictor(
    *,
    settings: Settings,
    effective_backend: str,
    threshold: float,
) -> PatchPredictor:
    inference_mode = getattr(settings, "bandon_inference_mode", "cli_per_tile")
    persistent_runner: Any | None = None

    def _get_persistent_runner():
        nonlocal persistent_runner
        if persistent_runner is None:
            from src.domain.bandon_runner import PersistentBandonRunner

            persistent_runner = PersistentBandonRunner(
                settings=settings,
                effective_backend=effective_backend,
                threshold=threshold,
            )
        return persistent_runner

    def _predict(
        *,
        tile: InferenceTile,
        t1_rgb: np.ndarray,
        t2_rgb: np.ndarray,
        t1_valid_mask: np.ndarray | None,
        t2_valid_mask: np.ndarray | None,
        aoi_mask: np.ndarray | None,
        work_dir: Path,
    ) -> PatchPrediction:
        from src.domain.bandon_runner import run_bandon_inference

        timing_enabled = bool(getattr(settings, "inference_timing_enabled", False))
        patch_write_started = time.perf_counter()
        work_dir.mkdir(parents=True, exist_ok=True)
        image_a_path = work_dir / "patch_t1.png"
        image_b_path = work_dir / "patch_t2.png"
        Image.fromarray(t1_rgb).save(image_a_path)
        Image.fromarray(t2_rgb).save(image_b_path)
        t1_valid_path = _write_mask_png(work_dir / "patch_t1_valid.png", t1_valid_mask) if t1_valid_mask is not None else None
        t2_valid_path = _write_mask_png(work_dir / "patch_t2_valid.png", t2_valid_mask) if t2_valid_mask is not None else None
        if aoi_mask is None and t2_valid_mask is not None:
            aoi_mask = np.ones(t2_valid_mask.shape, dtype=bool)
        aoi_path = _write_mask_png(work_dir / "patch_aoi.png", aoi_mask) if aoi_mask is not None else None
        patch_png_write_ms = elapsed_ms(patch_write_started)
        if inference_mode == "persistent_runner":
            result = _get_persistent_runner().predict_tile(
                image_a_path=image_a_path,
                image_b_path=image_b_path,
                out_dir=work_dir / "bandon_run",
                t1_valid_mask_path=t1_valid_path,
                t2_valid_mask_path=t2_valid_path,
                aoi_mask_path=aoi_path,
            )
        elif inference_mode == "cli_per_tile":
            result = run_bandon_inference(
                image_a_path=image_a_path,
                image_b_path=image_b_path,
                settings=settings,
                out_dir=work_dir / "bandon_run",
                t1_valid_mask_path=t1_valid_path,
                t2_valid_mask_path=t2_valid_path,
                aoi_mask_path=aoi_path,
                effective_backend=effective_backend,
                threshold=threshold,
            )
        else:
            raise ValueError(f"Unsupported BANDON inference mode: {inference_mode}")
        metadata = dict(result.metadata)
        if timing_enabled:
            runner_timing = result.parent_timing_ms or {}
            metadata["patch_predictor_timing_ms"] = {
                "patch_png_write_ms": patch_png_write_ms,
                "bandon_subprocess_wall_ms": runner_timing.get("subprocess_wall_ms"),
                "bandon_persistent_request_ms": runner_timing.get("persistent_worker_request_ms"),
                "bandon_persistent_startup_wall_ms": runner_timing.get("persistent_worker_startup_wall_ms"),
                "persistent_worker_rss_mb": runner_timing.get("persistent_worker_rss_mb"),
                "bandon_output_read_ms": runner_timing.get("bandon_output_read_ms"),
            }
        return PatchPrediction(
            probability=result.change_probability.astype(np.float32, copy=False),
            mask=threshold_change_probability(
                result.change_probability,
                change_threshold=threshold,
            ),
            metadata=metadata,
        )

    def _close() -> None:
        runner = persistent_runner
        if runner is not None:
            runner.close()

    setattr(_predict, "close", _close)
    return _predict


def make_difference_patch_predictor(*, threshold: float = 0.08) -> PatchPredictor:
    """Deterministic lightweight predictor for tests and cache-only smoke checks."""

    def _predict(
        *,
        tile: InferenceTile,
        t1_rgb: np.ndarray,
        t2_rgb: np.ndarray,
        t1_valid_mask: np.ndarray | None,
        t2_valid_mask: np.ndarray | None,
        aoi_mask: np.ndarray | None,
        work_dir: Path,
    ) -> PatchPrediction:
        diff = np.mean(np.abs(t2_rgb.astype(np.float32) - t1_rgb.astype(np.float32)), axis=2) / 255.0
        valid = np.ones(diff.shape, dtype=bool)
        if t1_valid_mask is not None:
            valid &= t1_valid_mask
        if t2_valid_mask is not None:
            valid &= t2_valid_mask
        if aoi_mask is not None:
            valid &= aoi_mask
        mask = (diff >= threshold) & valid
        return PatchPrediction(
            probability=diff.astype(np.float32),
            mask=mask,
            metadata={"predictor": "difference_smoke", "tile_index": tile.index},
        )

    return _predict


def make_synthetic_square_patch_predictor(*, every_n_tiles: int = 3) -> PatchPredictor:
    """Small deterministic positives for vector/resume validation without model cost."""

    def _predict(
        *,
        tile: InferenceTile,
        t1_rgb: np.ndarray,
        t2_rgb: np.ndarray,
        t1_valid_mask: np.ndarray | None,
        t2_valid_mask: np.ndarray | None,
        aoi_mask: np.ndarray | None,
        work_dir: Path,
    ) -> PatchPrediction:
        height, width = t2_rgb.shape[:2]
        probability = np.zeros((height, width), dtype=np.float32)
        mask = np.zeros((height, width), dtype=bool)
        if every_n_tiles > 0 and tile.index % every_n_tiles == 0:
            y0 = max(0, height // 2 - 16)
            x0 = max(0, width // 2 - 16)
            y1 = min(height, y0 + 32)
            x1 = min(width, x0 + 32)
            probability[y0:y1, x0:x1] = 0.95
            mask[y0:y1, x0:x1] = True
        if t1_valid_mask is not None:
            mask &= t1_valid_mask
        if t2_valid_mask is not None:
            mask &= t2_valid_mask
        if aoi_mask is not None:
            mask &= aoi_mask
        probability = np.where(mask, probability, 0.0).astype(np.float32)
        return PatchPrediction(
            probability=probability,
            mask=mask,
            metadata={"predictor": "synthetic_square", "tile_index": tile.index},
        )

    return _predict


def _validate_aligned_pair(t1: rasterio.io.DatasetReader, t2: rasterio.io.DatasetReader) -> None:
    mismatches: list[str] = []
    if t1.width != t2.width or t1.height != t2.height:
        mismatches.append(f"shape {t1.width}x{t1.height} != {t2.width}x{t2.height}")
    if t1.transform != t2.transform:
        mismatches.append("transform")
    if str(t1.crs) != str(t2.crs):
        mismatches.append(f"crs {t1.crs} != {t2.crs}")
    if mismatches:
        raise ValueError(f"Tiled inference requires aligned mosaics: {', '.join(mismatches)}")


def _write_feature_geojsonl(
    *,
    handle: Any,
    tile: InferenceTile,
    mask: np.ndarray,
    probability: np.ndarray,
    transform: Affine,
    crs: Any,
    release_t1: str,
    release_t2: str,
) -> int:
    count = 0
    for geom, value in shapes(mask.astype(np.uint8), mask=mask.astype(np.uint8), transform=transform):
        if int(value) != 1:
            continue
        geom_native = shape(geom)
        geom_wgs84 = reproject_geometry(geom_native, str(crs), "EPSG:4326")
        area_m2 = abs(GEOD.geometry_area_perimeter(geom_wgs84)[0])
        feature = {
            "type": "Feature",
            "geometry": mapping(geom_wgs84),
            "properties": {
                "tile_index": tile.index,
                "area_m2": float(area_m2),
                "release_t1": release_t1,
                "release_t2": release_t2,
                "mean_probability": float(np.mean(probability[mask])) if np.any(mask) else None,
            },
        }
        handle.write(json.dumps(feature, separators=(",", ":")) + "\n")
        count += 1
    return count


def _stats_from_histogram(sum_value: float, sum_sq: float, count: int, min_value: float | None, max_value: float | None) -> dict[str, float | None]:
    if count < 1:
        return {"min": None, "max": None, "mean": None, "std": None}
    mean = sum_value / count
    variance = max(0.0, (sum_sq / count) - (mean * mean))
    return {
        "min": min_value,
        "max": max_value,
        "mean": float(mean),
        "std": float(math.sqrt(variance)),
    }


def _numeric_metadata_value(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _flatten_child_timing(child_timing: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(child_timing, dict):
        return {}
    mapping = {
        "child_total_wall_ms": "child_total_wall_ms",
        "child_model_load_count_this_prediction": "model_load_count_this_prediction",
        "child_model_load_count_total": "model_load_count_total",
        "child_model_reused_numeric": "model_reused_numeric",
        "child_model_build_ms": "model_build_ms",
        "child_checkpoint_load_ms": "checkpoint_load_ms",
        "child_model_to_device_ms": "model_to_device_ms",
        "child_input_preprocess_ms": "input_preprocess_ms",
        "child_forward_total_ms": "forward_total_ms",
        "child_output_write_ms": "output_write_ms",
    }
    flattened: dict[str, float] = {}
    for output_key, child_key in mapping.items():
        value = _numeric_metadata_value(child_timing, child_key)
        if value is not None:
            flattened[output_key] = value
    return flattened


def _mean_from_records(records: list[dict[str, Any]], key: str) -> float | None:
    values = [float(record[key]) for record in records if isinstance(record.get(key), (int, float))]
    return float(sum(values) / len(values)) if values else None


def run_tiled_inference(
    *,
    t1_mosaic_path: Path,
    t2_mosaic_path: Path,
    t1_valid_mask_path: Path | None,
    t2_valid_mask_path: Path | None,
    output_dir: Path,
    run_id: str,
    settings: Settings,
    config: TiledInferenceConfig,
    predictor: PatchPredictor,
    aoi_geojson: dict[str, Any] | None = None,
    release_t1: str = "T1",
    release_t2: str = "T2",
    progress_callback: ProgressCallback | None = None,
    max_tiles: int | None = None,
) -> TiledInferenceResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = settings.runtime_cache_dir / "inference_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{run_id}.json"
    probability_path = output_dir / "prediction_change_probability.tif"
    mask_path = output_dir / "prediction_change_mask.tif"
    geojsonl_path = output_dir / "prediction_change_polygons.geojsonl"
    metadata_path = output_dir / "tiled_inference_metadata.json"
    timing_enabled = bool(getattr(settings, "inference_timing_enabled", False))
    timing_summary_path = output_dir / "timing_summary.json"
    timing_records: list[dict[str, Any]] = []
    if timing_enabled:
        LOGGER.info("INFERENCE_TIMING_ENABLED runId=%s summaryPath=%s", run_id, timing_summary_path)
    started = time.monotonic()
    completed_chunks: set[int] = set()
    can_resume = probability_path.exists() and mask_path.exists() and state_path.exists()
    if can_resume:
        try:
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            completed_values = state_payload.get("completed_chunks")
            if isinstance(completed_values, list):
                completed_chunks = {int(value) for value in completed_values}
        except Exception:
            completed_chunks = set()
    if completed_chunks:
        LOGGER.info(
            "TILED_INFERENCE_RESUME_STATE_LOADED runId=%s completedChunks=%s statePath=%s",
            run_id,
            len(completed_chunks),
            state_path,
        )

    with rasterio.Env(GDAL_CACHEMAX=128), rasterio.open(t1_mosaic_path, sharing=False) as t1_src, rasterio.open(
        t2_mosaic_path,
        sharing=False,
    ) as t2_src:
        _validate_aligned_pair(t1_src, t2_src)
        profile = t2_src.profile.copy()
        mask_profile = profile.copy()
        mask_profile.update(count=1, dtype="uint8", compress="deflate", tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
        probability_profile = mask_profile.copy()
        probability_profile.update(dtype="float32", nodata=0.0)
        tiles = iter_inference_tiles(
            t2_src.width,
            t2_src.height,
            tile_size=config.tile_size,
            overlap=config.overlap,
        )
        total_tiles = len(tiles)
        if max_tiles is not None:
            tiles = tiles[:max_tiles]
        selected_total = len(tiles)

        LOGGER.info(
            "TILED_INFERENCE_START runId=%s width=%s height=%s tileSize=%s overlap=%s stride=%s totalTiles=%s selectedTiles=%s",
            run_id,
            t2_src.width,
            t2_src.height,
            config.tile_size,
            config.overlap,
            config.stride,
            total_tiles,
            selected_total,
        )

        feature_count = 0
        sum_value = 0.0
        sum_sq = 0.0
        value_count = 0
        min_value: float | None = None
        max_value: float | None = None

        try:
            if probability_path.exists() and mask_path.exists() and completed_chunks:
                prob_cm = rasterio.open(probability_path, "r+")
                mask_cm = rasterio.open(mask_path, "r+")
                geojsonl_mode = "a"
            else:
                completed_chunks = set()
                prob_cm = rasterio.open(probability_path, "w", **probability_profile)
                mask_cm = rasterio.open(mask_path, "w", **mask_profile)
                geojsonl_mode = "w"
        except Exception:
            completed_chunks = set()
            prob_cm = rasterio.open(probability_path, "w", **probability_profile)
            mask_cm = rasterio.open(mask_path, "w", **mask_profile)
            geojsonl_mode = "w"

        processed_tiles = 0
        skipped_tiles = 0
        duration_samples: list[float] = []
        rss_samples: list[float] = []
        with prob_cm as prob_dst, mask_cm as mask_dst, geojsonl_path.open(geojsonl_mode, encoding="utf-8") as geojsonl:
            for ordinal, tile in enumerate(tiles, start=1):
                if tile.index in completed_chunks:
                    skipped_tiles += 1
                    continue
                tile_started = time.monotonic()
                tile_wall_started = time.perf_counter()
                parent_timing_ms: dict[str, float] = {
                    "raster_window_read_ms": 0.0,
                    "valid_mask_read_ms": 0.0,
                    "aoi_mask_build_ms": 0.0,
                    "patch_png_write_ms": 0.0,
                    "bandon_subprocess_wall_ms": 0.0,
                    "bandon_persistent_request_ms": 0.0,
                    "bandon_persistent_startup_wall_ms": 0.0,
                    "persistent_worker_rss_mb": 0.0,
                    "bandon_output_read_ms": 0.0,
                    "prediction_geotiff_write_ms": 0.0,
                    "vectorization_ms": 0.0,
                    "state_progress_write_ms": 0.0,
                    "other_parent_overhead_ms": 0.0,
                }
                window = tile.window.round_offsets().round_lengths()
                tile_dir = output_dir / "tiles" / f"{tile.index:06d}"
                read_started = time.perf_counter()
                t1_rgb = _read_rgb_window(t1_src, window)
                t2_rgb = _read_rgb_window(t2_src, window)
                if timing_enabled:
                    parent_timing_ms["raster_window_read_ms"] = elapsed_ms(read_started)
                mask_read_started = time.perf_counter()
                t1_valid = _read_mask_window(t1_valid_mask_path, window)
                t2_valid = _read_mask_window(t2_valid_mask_path, window)
                if timing_enabled:
                    parent_timing_ms["valid_mask_read_ms"] = elapsed_ms(mask_read_started)
                tile_transform = window_transform(window, t2_src.transform)
                aoi_started = time.perf_counter()
                aoi_mask = _aoi_mask_for_window(
                    aoi_geojson=aoi_geojson,
                    raster_crs=t2_src.crs,
                    out_shape=(int(window.height), int(window.width)),
                    transform=tile_transform,
                )
                if timing_enabled:
                    parent_timing_ms["aoi_mask_build_ms"] = elapsed_ms(aoi_started)

                prediction = predictor(
                    tile=tile,
                    t1_rgb=t1_rgb,
                    t2_rgb=t2_rgb,
                    t1_valid_mask=t1_valid,
                    t2_valid_mask=t2_valid,
                    aoi_mask=aoi_mask,
                    work_dir=tile_dir,
                )
                if timing_enabled:
                    patch_predictor_timing = prediction.metadata.get("patch_predictor_timing_ms")
                    if isinstance(patch_predictor_timing, dict):
                        for key in (
                            "patch_png_write_ms",
                            "bandon_subprocess_wall_ms",
                            "bandon_persistent_request_ms",
                            "bandon_persistent_startup_wall_ms",
                            "persistent_worker_rss_mb",
                            "bandon_output_read_ms",
                        ):
                            value = patch_predictor_timing.get(key)
                            if isinstance(value, (int, float)):
                                parent_timing_ms[key] = float(value)
                probability = prediction.probability.astype(np.float32, copy=False)
                mask = prediction.mask.astype(bool, copy=False)
                if probability.shape != (int(window.height), int(window.width)):
                    raise ValueError(
                        f"Tile {tile.index} probability shape {probability.shape} does not match window {(int(window.height), int(window.width))}."
                    )
                if mask.shape != probability.shape:
                    raise ValueError(f"Tile {tile.index} mask shape {mask.shape} does not match probability shape {probability.shape}.")
                if t1_valid is not None:
                    mask &= t1_valid
                if t2_valid is not None:
                    mask &= t2_valid
                if aoi_mask is not None:
                    mask &= aoi_mask
                    probability = np.where(aoi_mask, probability, 0.0).astype(np.float32)

                geotiff_write_started = time.perf_counter()
                prob_dst.write(probability, indexes=1, window=window)
                mask_dst.write(mask.astype(np.uint8), indexes=1, window=window)
                if timing_enabled:
                    parent_timing_ms["prediction_geotiff_write_ms"] = elapsed_ms(geotiff_write_started)
                processed_tiles += 1
                vectorization_started = time.perf_counter()
                feature_count += _write_feature_geojsonl(
                    handle=geojsonl,
                    tile=tile,
                    mask=mask,
                    probability=probability,
                    transform=tile_transform,
                    crs=t2_src.crs,
                    release_t1=release_t1,
                    release_t2=release_t2,
                )
                if timing_enabled:
                    parent_timing_ms["vectorization_ms"] = elapsed_ms(vectorization_started)

                finite = probability[np.isfinite(probability)]
                if finite.size:
                    sum_value += float(np.sum(finite))
                    sum_sq += float(np.sum(np.square(finite)))
                    value_count += int(finite.size)
                    tile_min = float(np.min(finite))
                    tile_max = float(np.max(finite))
                    min_value = tile_min if min_value is None else min(min_value, tile_min)
                    max_value = tile_max if max_value is None else max(max_value, tile_max)

                completed_chunks.add(int(tile.index))
                tile_duration = time.monotonic() - tile_started
                duration_samples.append(tile_duration)
                rss_value = _rss_mb()
                if rss_value is not None:
                    rss_samples.append(rss_value)
                completed_count = len(completed_chunks)
                elapsed = max(time.monotonic() - started, 1e-6)
                rate = max(processed_tiles, 1) / elapsed
                remaining_count = max(selected_total - completed_count, 0)
                eta = remaining_count / rate if rate > 0 else None
                state = {
                    "run_id": run_id,
                    "processed_tiles": completed_count,
                    "processed_tiles_this_run": processed_tiles,
                    "skipped_tiles_this_run": skipped_tiles,
                    "total_tiles": selected_total,
                    "full_tile_count": total_tiles,
                    "last_tile_index": tile.index,
                    "completed_chunks": sorted(completed_chunks),
                    "completed_chunk_count": completed_count,
                    "tile_rate_per_sec": rate,
                    "eta_seconds": eta,
                    "rss_mb": rss_value,
                    "tile_duration_seconds": tile_duration,
                    "probability_path": str(probability_path),
                    "mask_path": str(mask_path),
                    "geojsonl_path": str(geojsonl_path),
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                if timing_enabled:
                    child_timing = prediction.metadata.get("child_timing_ms")
                    child_timing_flat = _flatten_child_timing(child_timing if isinstance(child_timing, dict) else None)
                    timing_record: dict[str, Any] = {
                        "tile_id": tile.index,
                        "run_id": run_id,
                        **parent_timing_ms,
                        **child_timing_flat,
                    }
                    timing_record["tile_total_wall_ms"] = elapsed_ms(tile_wall_started)
                    measured_parent_ms = sum(
                        float(timing_record.get(key) or 0.0)
                        for key in (
                            "raster_window_read_ms",
                            "valid_mask_read_ms",
                            "aoi_mask_build_ms",
                            "patch_png_write_ms",
                            "bandon_subprocess_wall_ms",
                            "bandon_persistent_request_ms",
                            "bandon_persistent_startup_wall_ms",
                            "bandon_output_read_ms",
                            "prediction_geotiff_write_ms",
                            "vectorization_ms",
                        )
                    )
                    timing_record["other_parent_overhead_ms"] = round(max(float(timing_record["tile_total_wall_ms"]) - measured_parent_ms, 0.0), 3)
                    parent_timing_ms["tile_total_wall_ms"] = float(timing_record["tile_total_wall_ms"])
                    parent_timing_ms["other_parent_overhead_ms"] = float(timing_record["other_parent_overhead_ms"])
                    state.update(
                        {
                            "last_tile_total_wall_ms": timing_record.get("tile_total_wall_ms"),
                            "last_bandon_subprocess_wall_ms": timing_record.get("bandon_subprocess_wall_ms"),
                            "last_child_forward_total_ms": timing_record.get("child_forward_total_ms"),
                        }
                    )
                state_progress_started = time.perf_counter()
                state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "tiled_inference",
                            "processed_tiles": completed_count,
                            "processed_tiles_this_run": processed_tiles,
                            "skipped_tiles_this_run": skipped_tiles,
                            "total_tiles": selected_total,
                            "full_tile_count": total_tiles,
                            "tile_rate_per_sec": rate,
                            "eta_seconds": eta,
                            "rss_mb": rss_value,
                            "tile_duration_seconds": tile_duration,
                            "current_tile_index": tile.index,
                        }
                    )
                if timing_enabled:
                    parent_timing_ms["state_progress_write_ms"] = elapsed_ms(state_progress_started)
                    timing_record["state_progress_write_ms"] = parent_timing_ms["state_progress_write_ms"]
                    timing_record["tile_total_wall_ms"] = elapsed_ms(tile_wall_started)
                    timing_record["other_parent_overhead_ms"] = round(
                        max(
                            float(timing_record["tile_total_wall_ms"])
                            - sum(
                                float(timing_record.get(key) or 0.0)
                                for key in (
                                    "raster_window_read_ms",
                                    "valid_mask_read_ms",
                                    "aoi_mask_build_ms",
                                    "patch_png_write_ms",
                                    "bandon_subprocess_wall_ms",
                                    "bandon_persistent_request_ms",
                                    "bandon_persistent_startup_wall_ms",
                                    "bandon_output_read_ms",
                                    "prediction_geotiff_write_ms",
                                    "vectorization_ms",
                                    "state_progress_write_ms",
                                )
                            ),
                            0.0,
                        ),
                        3,
                    )
                    parent_timing_ms["tile_total_wall_ms"] = float(timing_record["tile_total_wall_ms"])
                    parent_timing_ms["other_parent_overhead_ms"] = float(timing_record["other_parent_overhead_ms"])
                    timing_records.append(timing_record)
                    safe_merge_json_file(
                        tile_dir / "bandon_run" / "run_metadata.json",
                        {
                            "run_id": run_id,
                            "tile_id": tile.index,
                            "timing_enabled": True,
                            "parent_timing_ms": parent_timing_ms,
                            "child_timing_ms": prediction.metadata.get("child_timing_ms"),
                            "bandon_runner_timing_ms": prediction.metadata.get("bandon_runner_timing_ms"),
                        },
                    )
                    state.update(
                        {
                            "last_tile_total_wall_ms": timing_record.get("tile_total_wall_ms"),
                            "last_bandon_subprocess_wall_ms": timing_record.get("bandon_subprocess_wall_ms"),
                            "last_child_forward_total_ms": timing_record.get("child_forward_total_ms"),
                            "mean_tile_total_wall_ms": _mean_from_records(timing_records, "tile_total_wall_ms"),
                            "mean_bandon_subprocess_wall_ms": _mean_from_records(timing_records, "bandon_subprocess_wall_ms"),
                            "mean_child_forward_total_ms": _mean_from_records(timing_records, "child_forward_total_ms"),
                        }
                    )
                    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                    if processed_tiles % 25 == 0:
                        write_timing_summary(
                            timing_summary_path,
                            run_id=run_id,
                            records=timing_records,
                            fields=TIMING_SUMMARY_FIELDS,
                        )
                LOGGER.info(
                    "TILED_INFERENCE_PROGRESS runId=%s processed=%s total=%s rate=%.3f rssMb=%s",
                    run_id,
                    completed_count,
                    selected_total,
                    rate,
                    state.get("rss_mb"),
                )
        final_elapsed = max(time.monotonic() - started, 1e-6)
        final_rate = processed_tiles / final_elapsed if processed_tiles > 0 else 0.0
        final_state = {
            "run_id": run_id,
            "processed_tiles": len(completed_chunks),
            "processed_tiles_this_run": processed_tiles,
            "skipped_tiles_this_run": skipped_tiles,
            "total_tiles": selected_total,
            "full_tile_count": total_tiles,
            "completed_chunks": sorted(completed_chunks),
            "completed_chunk_count": len(completed_chunks),
            "tile_rate_per_sec": final_rate,
            "eta_seconds": 0.0 if len(completed_chunks) >= selected_total else None,
            "rss_mb": _rss_mb(),
            "probability_path": str(probability_path),
            "mask_path": str(mask_path),
            "geojsonl_path": str(geojsonl_path),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if timing_enabled:
            final_state.update(
                {
                    "mean_tile_total_wall_ms": _mean_from_records(timing_records, "tile_total_wall_ms"),
                    "mean_bandon_subprocess_wall_ms": _mean_from_records(timing_records, "bandon_subprocess_wall_ms"),
                    "mean_child_forward_total_ms": _mean_from_records(timing_records, "child_forward_total_ms"),
                    "timing_summary_path": str(timing_summary_path),
                }
            )
            write_timing_summary(
                timing_summary_path,
                run_id=run_id,
                records=timing_records,
                fields=TIMING_SUMMARY_FIELDS,
            )
        state_path.write_text(
            json.dumps(final_state, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    close_predictor = getattr(predictor, "close", None)
    if callable(close_predictor):
        close_predictor()

    probability_stats = _stats_from_histogram(sum_value, sum_sq, value_count, min_value, max_value)
    metadata = {
        "run_id": run_id,
        "mode": "tiled",
        "tile_size": config.tile_size,
        "overlap": config.overlap,
        "stride": config.stride,
        "batch_size": config.batch_size,
        "threshold": config.threshold,
        "processed_tiles": len(completed_chunks),
        "processed_tiles_this_run": processed_tiles,
        "skipped_tiles_this_run": skipped_tiles,
        "total_tiles": total_tiles,
        "selected_tiles": selected_total,
        "completed_chunks": sorted(completed_chunks),
        "completed_chunk_count": len(completed_chunks),
        "feature_count": feature_count,
        "probability_stats": probability_stats,
        "tile_duration_seconds": {
            "min": min(duration_samples) if duration_samples else None,
            "mean": float(sum(duration_samples) / len(duration_samples)) if duration_samples else None,
            "max": max(duration_samples) if duration_samples else None,
            "count": len(duration_samples),
        },
        "rss_mb_samples": {
            "min": min(rss_samples) if rss_samples else None,
            "mean": float(sum(rss_samples) / len(rss_samples)) if rss_samples else None,
            "max": max(rss_samples) if rss_samples else None,
            "count": len(rss_samples),
        },
        "t1_mosaic_path": str(t1_mosaic_path),
        "t2_mosaic_path": str(t2_mosaic_path),
        "probability_path": str(probability_path),
        "mask_path": str(mask_path),
        "geojsonl_path": str(geojsonl_path),
        "rss_mb": _rss_mb(),
        "duration_seconds": time.monotonic() - started,
    }
    if timing_enabled:
        metadata["timing_enabled"] = True
        metadata["timing_summary_path"] = str(timing_summary_path)
        metadata["timing_record_count"] = len(timing_records)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info(
        "TILED_INFERENCE_DONE runId=%s processedTiles=%s totalTiles=%s features=%s probabilityPath=%s maskPath=%s",
        run_id,
        selected_total,
        total_tiles,
        feature_count,
        probability_path,
        mask_path,
    )
    return TiledInferenceResult(
        probability_path=probability_path,
        mask_path=mask_path,
        geojsonl_path=geojsonl_path,
        metadata_path=metadata_path,
        state_path=state_path,
        feature_count=feature_count,
        processed_tiles=len(completed_chunks),
        skipped_tiles=skipped_tiles,
        total_tiles=total_tiles,
        probability_stats=probability_stats,
        metadata=metadata,
    )
