from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
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
_WAYBACK_MOSAIC_CACHE_VERSION = 2
_CACHE_PNG_NAME = "mosaic.png"
_CACHE_TIF_NAME = "mosaic.tif"
_CACHE_VALID_MASK_NAME = "valid_mask.tif"
_CACHE_METADATA_NAME = "metadata.json"
_CACHE_LOCK_SUFFIX = ".lock"
_CACHE_LOCK_TIMEOUT_SEC = 30.0
_CACHE_LOCK_POLL_INTERVAL_SEC = 0.05


@dataclass(frozen=True)
class TileDownloadResult:
    status: str
    content: bytes | None = None


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


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _wayback_mosaic_cache_key(
    *,
    release: WaybackRelease,
    tile_matrix_set: str,
    zoom: int,
    tile_range: tuple[int, int, int, int],
) -> str:
    payload = {
        "version": _WAYBACK_MOSAIC_CACHE_VERSION,
        "provider": "wayback",
        "release_identifier": release.identifier,
        "release_num": release.release_num,
        "tile_matrix_set": tile_matrix_set,
        "zoom": zoom,
        "tile_range": list(tile_range),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def _request_mosaic_paths(
    out_dir: Path,
    *,
    label: str,
    release_identifier: str,
    zoom: int,
) -> tuple[Path, Path, Path]:
    return (
        out_dir / f"{label}_{release_identifier}_z{zoom}.png",
        out_dir / f"{label}_{release_identifier}_z{zoom}.tif",
        out_dir / f"{label}_{release_identifier}_z{zoom}_valid_mask.tif",
    )


def _cache_mosaic_paths(cache_dir: Path) -> tuple[Path, Path, Path]:
    return (
        cache_dir / _CACHE_PNG_NAME,
        cache_dir / _CACHE_TIF_NAME,
        cache_dir / _CACHE_VALID_MASK_NAME,
    )


def _materialize_cached_file(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()
    try:
        os.link(source_path, target_path)
    except OSError:
        shutil.copy2(source_path, target_path)


def _materialize_cached_mosaic(
    cache_dir: Path,
    out_dir: Path,
    *,
    label: str,
    release: WaybackRelease,
    zoom: int,
) -> tuple[Path, Path, Path]:
    cache_png_path, cache_tif_path, cache_valid_mask_path = _cache_mosaic_paths(cache_dir)
    _, tif_path, valid_mask_path = _request_mosaic_paths(
        out_dir,
        label=label,
        release_identifier=release.identifier,
        zoom=zoom,
    )
    for source_path, target_path in ((cache_tif_path, tif_path), (cache_valid_mask_path, valid_mask_path)):
        _materialize_cached_file(source_path, target_path)
    return cache_png_path, tif_path, valid_mask_path


def _cache_lock_dir(cache_dir: Path) -> Path:
    return cache_dir.with_name(f"{cache_dir.name}{_CACHE_LOCK_SUFFIX}")


@contextmanager
def _acquire_cache_lock(cache_dir: Path):
    lock_dir = _cache_lock_dir(cache_dir)
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_SEC
    while True:
        try:
            lock_dir.mkdir(parents=False)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for mosaic cache lock {lock_dir}.")
            time.sleep(_CACHE_LOCK_POLL_INTERVAL_SEC)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _actual_available_tiles_from_metadata(metadata: dict[str, object]) -> list[list[int]] | None:
    actual_available_tiles = metadata.get("actual_available_tiles")
    if actual_available_tiles is None:
        return None
    if not isinstance(actual_available_tiles, list):
        return None
    normalized: list[list[int]] = []
    for tile in actual_available_tiles:
        if not (
            isinstance(tile, list)
            and len(tile) == 2
            and all(isinstance(coord, int) for coord in tile)
        ):
            return None
        normalized.append(tile)
    return normalized


def _build_cache_metadata(
    *,
    release: WaybackRelease,
    tile_matrix_set: str,
    zoom: int,
    tile_range: tuple[int, int, int, int],
    bounds_3857: tuple[float, float, float, float],
    tile_count: int,
    available_tile_count: int,
    missing_tile_count: int,
    available_tiles: frozenset[tuple[int, int]] | None,
    actual_available_tiles: frozenset[tuple[int, int]],
    actual_missing_tile_count: int,
    transient_failure_count: int,
    preflight_used: bool,
    reusable: bool,
    width: int,
    height: int,
) -> dict[str, object]:
    return {
        "version": _WAYBACK_MOSAIC_CACHE_VERSION,
        "release_identifier": release.identifier,
        "release_date": str(release.release_date),
        "release_num": release.release_num,
        "tile_matrix_set": tile_matrix_set,
        "zoom": zoom,
        "tile_range": list(tile_range),
        "bounds_3857": list(bounds_3857),
        "tile_count": tile_count,
        "available_tile_count": available_tile_count,
        "missing_tile_count": missing_tile_count,
        "available_tiles": (
            None
            if available_tiles is None
            else [list(tile) for tile in sorted(available_tiles)]
        ),
        "actual_available_tiles": [list(tile) for tile in sorted(actual_available_tiles)],
        "actual_available_tile_count": available_tile_count,
        "actual_missing_tile_count": actual_missing_tile_count,
        "transient_failure_count": transient_failure_count,
        "preflight_used": preflight_used,
        "reusable": reusable,
        "width": width,
        "height": height,
    }


def _valid_cache_metadata(
    metadata: dict[str, object],
    *,
    release: WaybackRelease,
    tile_matrix_set: str,
    zoom: int,
    tile_range: tuple[int, int, int, int],
    width: int,
    height: int,
) -> bool:
    bounds_3857 = metadata.get("bounds_3857")
    if not isinstance(bounds_3857, list) or len(bounds_3857) != 4:
        return False
    for field_name in (
        "tile_count",
        "available_tile_count",
        "missing_tile_count",
        "actual_available_tile_count",
        "actual_missing_tile_count",
        "transient_failure_count",
    ):
        if not isinstance(metadata.get(field_name), int):
            return False
    if not isinstance(metadata.get("preflight_used"), bool):
        return False
    if not isinstance(metadata.get("reusable"), bool):
        return False
    if _actual_available_tiles_from_metadata(metadata) is None:
        return False
    return (
        metadata.get("version") == _WAYBACK_MOSAIC_CACHE_VERSION
        and metadata.get("release_identifier") == release.identifier
        and metadata.get("release_num") == release.release_num
        and metadata.get("tile_matrix_set") == tile_matrix_set
        and metadata.get("zoom") == zoom
        and metadata.get("tile_range") == list(tile_range)
        and metadata.get("width") == width
        and metadata.get("height") == height
    )


def _cached_metadata_matches_request(
    metadata: dict[str, object],
    *,
    requested_available_tiles: frozenset[tuple[int, int]] | None,
) -> bool:
    if metadata.get("reusable") is not True:
        return False
    if requested_available_tiles is None:
        return True
    actual_available_tiles = _actual_available_tiles_from_metadata(metadata)
    expected_available_tiles = [list(tile) for tile in sorted(requested_available_tiles)]
    return actual_available_tiles == expected_available_tiles


def _load_cached_mosaic_metadata(
    cache_dir: Path,
    *,
    release: WaybackRelease,
    tile_matrix_set: str,
    zoom: int,
    tile_range: tuple[int, int, int, int],
    width: int,
    height: int,
) -> dict[str, object] | None:
    metadata_path = cache_dir / _CACHE_METADATA_NAME
    cache_png_path, cache_tif_path, cache_valid_mask_path = _cache_mosaic_paths(cache_dir)
    if not (
        metadata_path.exists()
        and cache_png_path.exists()
        and cache_tif_path.exists()
        and cache_valid_mask_path.exists()
    ):
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    if not _valid_cache_metadata(
        metadata,
        release=release,
        tile_matrix_set=tile_matrix_set,
        zoom=zoom,
        tile_range=tile_range,
        width=width,
        height=height,
    ):
        return None
    return metadata


def _cache_block_size(size: int) -> int:
    if size >= 512:
        return 512
    if size >= 256:
        return 256
    return max(16, ((size + 15) // 16) * 16)


def _write_cached_mosaic(
    *,
    staging_dir: Path,
    canvas: Image.Image,
    valid_mask: np.ndarray,
    transform,
    metadata: dict[str, object],
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    png_path = staging_dir / _CACHE_PNG_NAME
    tif_path = staging_dir / _CACHE_TIF_NAME
    valid_mask_path = staging_dir / _CACHE_VALID_MASK_NAME
    metadata_path = staging_dir / _CACHE_METADATA_NAME

    canvas.save(png_path)
    arr = np.asarray(canvas)
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
        compress="LZW",
        tiled=True,
        blockxsize=_cache_block_size(arr.shape[1]),
        blockysize=_cache_block_size(arr.shape[0]),
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
        compress="LZW",
        tiled=True,
        blockxsize=_cache_block_size(valid_mask.shape[1]),
        blockysize=_cache_block_size(valid_mask.shape[0]),
    ) as dst:
        dst.write(valid_mask, 1)
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _publish_cached_mosaic(
    *,
    cache_dir: Path,
    staging_dir: Path,
    release: WaybackRelease,
    tile_matrix_set: str,
    zoom: int,
    tile_range: tuple[int, int, int, int],
    width: int,
    height: int,
) -> None:
    try:
        staging_dir.replace(cache_dir)
        return
    except FileExistsError:
        pass
    except OSError:
        if not cache_dir.exists():
            raise

    cached_metadata = _load_cached_mosaic_metadata(
        cache_dir,
        release=release,
        tile_matrix_set=tile_matrix_set,
        zoom=zoom,
        tile_range=tile_range,
        width=width,
        height=height,
    )
    if cached_metadata is None:
        shutil.rmtree(cache_dir, ignore_errors=True)
        staging_dir.replace(cache_dir)
        return
    shutil.rmtree(staging_dir, ignore_errors=True)


def _download_tile_with_retries(url: str, settings: Settings) -> TileDownloadResult:
    attempts = max(settings.download_retries, 0) + 1
    backoff_sec = max(settings.download_retry_backoff_initial_sec, 0.0)
    for attempt in range(1, attempts + 1):
        try:
            tile_bytes = _download_tile(url, settings.request_timeout_sec)
            if tile_bytes is None:
                return TileDownloadResult(status="missing_404")
            return TileDownloadResult(status="available", content=tile_bytes)
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
            return TileDownloadResult(status="transient_failed")
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
            if response is not None and response.status_code == 404:
                return TileDownloadResult(status="missing_404")
            raise
    return TileDownloadResult(status="transient_failed")


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

    tile_range = (x_min, x_max, y_min, y_max)
    width = (x_max - x_min + 1) * 256
    height = (y_max - y_min + 1) * 256
    left, _, _, top = tile_bounds_3857(x_min, y_min, settings.zoom)
    _, bottom, right, _ = tile_bounds_3857(x_max, y_max, settings.zoom)
    bounds_3857 = (left, bottom, right, top)

    cache_key = _wayback_mosaic_cache_key(
        release=release,
        tile_matrix_set=settings.tile_matrix_set,
        zoom=settings.zoom,
        tile_range=tile_range,
    )
    cache_dir = settings.wayback_mosaic_cache_dir / cache_key
    with _acquire_cache_lock(cache_dir):
        cached_metadata = _load_cached_mosaic_metadata(
            cache_dir,
            release=release,
            tile_matrix_set=settings.tile_matrix_set,
            zoom=settings.zoom,
            tile_range=tile_range,
            width=width,
            height=height,
        )
        if cached_metadata is not None and _cached_metadata_matches_request(
            cached_metadata,
            requested_available_tiles=available_tiles,
        ):
            png_path, tif_path, valid_mask_path = _materialize_cached_mosaic(
                cache_dir,
                out_dir,
                label=label,
                release=release,
                zoom=settings.zoom,
            )
            return MosaicResult(
                identifier=release.identifier,
                release_date=str(release.release_date),
                tile_count=int(cached_metadata["tile_count"]),
                available_tile_count=int(cached_metadata["actual_available_tile_count"]),
                missing_tile_count=int(cached_metadata["actual_missing_tile_count"]),
                tile_range=tile_range,
                bounds_3857=tuple(float(value) for value in cached_metadata["bounds_3857"]),  # type: ignore[arg-type]
                png_path=png_path,
                geotiff_path=tif_path,
                valid_mask_path=valid_mask_path,
            )
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

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
            actual_available_coords: set[tuple[int, int]] = set()
            available_tile_count = 0
            missing_tile_count = skipped_missing_from_preflight
            transient_failure_count = 0
            for future in as_completed(future_map):
                x, y = future_map[future]
                tile_result = future.result()
                if tile_result.status == "missing_404":
                    missing_tile_count += 1
                    continue
                if tile_result.status == "transient_failed":
                    transient_failure_count += 1
                    continue
                if tile_result.content is None:
                    transient_failure_count += 1
                    continue
                available_tile_count += 1
                actual_available_coords.add((x, y))
                tile = Image.open(io.BytesIO(tile_result.content)).convert("RGB")
                canvas.paste(tile, ((x - x_min) * 256, (y - y_min) * 256))
                valid_mask[(y - y_min) * 256 : (y - y_min + 1) * 256, (x - x_min) * 256 : (x - x_min + 1) * 256] = 1

        if available_tile_count == 0:
            raise ValueError(
                f"Selected Wayback release {release.identifier} has no available imagery tiles for the requested AOI at z={settings.zoom}."
            )

        if available_tiles is not None and transient_failure_count == 0:
            actual_available_coords = set(available_tiles)

        arr = np.asarray(canvas)
        transform = from_bounds(*bounds_3857, width=arr.shape[1], height=arr.shape[0])
        metadata = _build_cache_metadata(
            release=release,
            tile_matrix_set=settings.tile_matrix_set,
            zoom=settings.zoom,
            tile_range=tile_range,
            bounds_3857=bounds_3857,
            tile_count=tile_count,
            available_tile_count=available_tile_count,
            missing_tile_count=missing_tile_count,
            available_tiles=available_tiles,
            actual_available_tiles=frozenset(actual_available_coords),
            actual_missing_tile_count=missing_tile_count,
            transient_failure_count=transient_failure_count,
            preflight_used=available_tiles is not None,
            reusable=transient_failure_count == 0,
            width=width,
            height=height,
        )
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f"{cache_key}-",
                dir=str(settings.wayback_mosaic_cache_dir),
            )
        )
        try:
            _write_cached_mosaic(
                staging_dir=staging_dir,
                canvas=canvas,
                valid_mask=valid_mask,
                transform=transform,
                metadata=metadata,
            )
            _publish_cached_mosaic(
                cache_dir=cache_dir,
                staging_dir=staging_dir,
                release=release,
                tile_matrix_set=settings.tile_matrix_set,
                zoom=settings.zoom,
                tile_range=tile_range,
                width=width,
                height=height,
            )
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        png_path, tif_path, valid_mask_path = _materialize_cached_mosaic(
            cache_dir,
            out_dir,
            label=label,
            release=release,
            zoom=settings.zoom,
        )

    return MosaicResult(
        identifier=release.identifier,
        release_date=str(release.release_date),
        tile_count=tile_count,
        available_tile_count=available_tile_count,
        missing_tile_count=missing_tile_count,
        tile_range=tile_range,
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
