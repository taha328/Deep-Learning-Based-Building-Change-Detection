from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import time

import numpy as np
import requests
from PIL import Image
import rasterio
from rasterio.transform import from_bounds

from src.config import Settings
from src.domain.coregistration import CoregistrationDiagnostics, coregister_t1_to_t2_with_arosics
from src.domain.tiling import tile_bounds_3857, tile_range_for_bbox
from src.domain.wayback import WaybackRelease
from src.utils.raster import align_rgb_to_reference, align_single_band_mask_to_reference, read_rgb

logger = logging.getLogger(__name__)
_RETRYABLE_DOWNLOAD_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class MosaicResult:
    identifier: str
    release_date: str
    tile_count: int
    available_tile_count: int
    missing_tile_count: int
    tile_range: tuple[int, int, int, int]
    bounds_3857: tuple[float, float, float, float]
    png_path: Path
    geotiff_path: Path
    valid_mask_path: Path


@dataclass(frozen=True)
class AlignmentResult:
    t1_rgb: np.ndarray
    t2_rgb: np.ndarray
    t1_valid_mask: np.ndarray
    t2_valid_mask: np.ndarray
    diagnostics: dict[str, object]


def build_tile_url(template: str, tile_matrix_set: str, zoom: int, x: int, y: int) -> str:
    return (
        template.replace("{TileMatrixSet}", tile_matrix_set)
        .replace("{TileMatrix}", str(zoom))
        .replace("{TileRow}", str(y))
        .replace("{TileCol}", str(x))
    )


def _download_tile(url: str, timeout_sec: int) -> bytes | None:
    response = requests.get(url, timeout=timeout_sec)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content


def _download_tile_with_retries(url: str, settings: Settings) -> bytes | None:
    attempts = max(settings.download_retries, 0) + 1
    backoff_sec = max(settings.download_retry_backoff_initial_sec, 0.0)
    for attempt in range(1, attempts + 1):
        try:
            return _download_tile(url, settings.request_timeout_sec)
        except (requests.ConnectTimeout, requests.ReadTimeout, requests.ConnectionError) as exc:
            if attempt < attempts:
                logger.warning(
                    "Retrying Wayback tile download after %s (%s/%s): %s",
                    type(exc).__name__,
                    attempt,
                    attempts,
                    url,
                )
                if backoff_sec > 0:
                    time.sleep(min(backoff_sec, settings.download_retry_backoff_max_sec))
                    backoff_sec = min(
                        max(backoff_sec * 2.0, settings.download_retry_backoff_initial_sec),
                        settings.download_retry_backoff_max_sec,
                    )
                continue
            logger.warning(
                "Wayback tile download failed after %s attempts; treating tile as missing: %s (%s)",
                attempts,
                url,
                type(exc).__name__,
            )
            return None
        except requests.HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code in _RETRYABLE_DOWNLOAD_STATUSES and attempt < attempts:
                logger.warning(
                    "Retrying Wayback tile download after HTTP %s (%s/%s): %s",
                    response.status_code,
                    attempt,
                    attempts,
                    url,
                )
                if backoff_sec > 0:
                    time.sleep(min(backoff_sec, settings.download_retry_backoff_max_sec))
                    backoff_sec = min(
                        max(backoff_sec * 2.0, settings.download_retry_backoff_initial_sec),
                        settings.download_retry_backoff_max_sec,
                    )
                continue
            raise


def download_wayback_mosaic(
    release: WaybackRelease,
    bbox: dict[str, float],
    *,
    settings: Settings,
    out_dir: Path,
    label: str,
    max_tiles: int | None = None,
    available_tiles: frozenset[tuple[int, int]] | None = None,
) -> MosaicResult:
    if settings.tile_matrix_set not in release.tile_matrix_sets:
        raise ValueError(f"{settings.tile_matrix_set} is not available for release {release.identifier}.")

    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, settings.zoom)
    tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
    if max_tiles is not None and tile_count > max_tiles:
        raise ValueError(
            f"AOI would download {tile_count} tiles for {release.identifier} at z={settings.zoom}; "
            "reduce the AOI or switch modes."
        )

    width = (x_max - x_min + 1) * 256
    height = (y_max - y_min + 1) * 256
    canvas = Image.new("RGB", (width, height))
    valid_mask = np.zeros((height, width), dtype=np.uint8)

    jobs = []
    skipped_missing_from_preflight = 0
    for y in range(y_min, y_max + 1):
        for x in range(x_min, x_max + 1):
            if available_tiles is not None and (x, y) not in available_tiles:
                skipped_missing_from_preflight += 1
                continue
            jobs.append(
                (
                    x,
                    y,
                    build_tile_url(release.resource_url_template, settings.tile_matrix_set, settings.zoom, x, y),
                )
            )

    with ThreadPoolExecutor(max_workers=settings.download_workers) as executor:
        future_map = {
            executor.submit(_download_tile_with_retries, tile_url, settings): (x, y)
            for x, y, tile_url in jobs
        }
        available_tile_count = 0
        missing_tile_count = skipped_missing_from_preflight
        for future in as_completed(future_map):
            x, y = future_map[future]
            tile_bytes = future.result()
            if tile_bytes is None:
                missing_tile_count += 1
                continue
            available_tile_count += 1
            tile = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
            canvas.paste(tile, ((x - x_min) * 256, (y - y_min) * 256))
            valid_mask[(y - y_min) * 256 : (y - y_min + 1) * 256, (x - x_min) * 256 : (x - x_min + 1) * 256] = 1

    if available_tile_count == 0:
        raise ValueError(
            f"Selected Wayback release {release.identifier} has no available imagery tiles for the requested AOI at z={settings.zoom}."
        )

    left, _, _, top = tile_bounds_3857(x_min, y_min, settings.zoom)
    _, bottom, right, _ = tile_bounds_3857(x_max, y_max, settings.zoom)
    bounds_3857 = (left, bottom, right, top)

    png_path = out_dir / f"{label}_{release.identifier}_z{settings.zoom}.png"
    tif_path = out_dir / f"{label}_{release.identifier}_z{settings.zoom}.tif"
    valid_mask_path = out_dir / f"{label}_{release.identifier}_z{settings.zoom}_valid_mask.tif"
    canvas.save(png_path)

    arr = np.asarray(canvas)
    transform = from_bounds(*bounds_3857, width=arr.shape[1], height=arr.shape[0])
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        width=arr.shape[1],
        height=arr.shape[0],
        count=3,
        dtype=arr.dtype,
        crs="EPSG:3857",
        transform=transform,
        ) as dst:
            for band_index in range(3):
                dst.write(arr[:, :, band_index], band_index + 1)
    with rasterio.open(
        valid_mask_path,
        "w",
        driver="GTiff",
        width=valid_mask.shape[1],
        height=valid_mask.shape[0],
        count=1,
        dtype=valid_mask.dtype,
        crs="EPSG:3857",
        transform=transform,
    ) as dst:
        dst.write(valid_mask, 1)

    return MosaicResult(
        identifier=release.identifier,
        release_date=str(release.release_date),
        tile_count=tile_count,
        available_tile_count=available_tile_count,
        missing_tile_count=missing_tile_count,
        tile_range=(x_min, x_max, y_min, y_max),
        bounds_3857=bounds_3857,
        png_path=png_path,
        geotiff_path=tif_path,
        valid_mask_path=valid_mask_path,
    )


def _same_raster_grid(source_path: Path, reference_path: Path) -> bool:
    with rasterio.open(source_path) as src, rasterio.open(reference_path) as ref:
        return (
            src.width == ref.width
            and src.height == ref.height
            and src.crs == ref.crs
            and src.transform == ref.transform
        )


def align_mosaic_pair(
    t1_mosaic: MosaicResult,
    t2_mosaic: MosaicResult,
    *,
    settings: Settings,
    out_dir: Path,
) -> AlignmentResult:
    t2_rgb = read_rgb(t2_mosaic.geotiff_path)
    t2_valid_mask = read_rgb(t2_mosaic.valid_mask_path)[:, :, 0] > 0
    coreg_result = coregister_t1_to_t2_with_arosics(
        reference_image_path=t2_mosaic.geotiff_path,
        target_image_path=t1_mosaic.geotiff_path,
        reference_valid_mask_path=t2_mosaic.valid_mask_path,
        target_valid_mask_path=t1_mosaic.valid_mask_path,
        output_dir=out_dir,
        settings=settings,
    )

    source_t1_path = coreg_result.corrected_t1_path
    source_t1_valid_mask_path = coreg_result.corrected_t1_valid_mask_path

    if _same_raster_grid(source_t1_path, t2_mosaic.geotiff_path):
        t1_rgb = read_rgb(source_t1_path)
        t1_valid_mask = read_rgb(source_t1_valid_mask_path)[:, :, 0] > 0
    else:
        t1_rgb = align_rgb_to_reference(source_t1_path, t2_mosaic.geotiff_path)
        t1_valid_mask = align_single_band_mask_to_reference(source_t1_valid_mask_path, t2_mosaic.geotiff_path)

    diagnostics = coreg_result.diagnostics.to_dict()
    diagnostics["aligned_to_reference_grid"] = _same_raster_grid(source_t1_path, t2_mosaic.geotiff_path)

    return AlignmentResult(
        t1_rgb=t1_rgb,
        t2_rgb=t2_rgb,
        t1_valid_mask=t1_valid_mask,
        t2_valid_mask=t2_valid_mask,
        diagnostics=diagnostics,
    )
