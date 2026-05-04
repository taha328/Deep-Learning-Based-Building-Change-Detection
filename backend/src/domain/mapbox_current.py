from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import numpy as np
from PIL import Image
import rasterio
from rasterio.transform import from_bounds
import requests

from src.config import Settings
from src.domain.mosaic import MosaicResult
from src.domain.tiling import tile_bounds_3857, tile_range_for_bbox


MAPBOX_PROVIDER = "mapbox"
MAPBOX_SOURCE_TYPE = "current_basemap"
MAPBOX_SOURCE_ID = "mapbox.satellite"
MAPBOX_ATTRIBUTION = "© Mapbox © OpenStreetMap © Maxar"
_CACHE_VERSION = 1
_CACHE_PNG_NAME = "mosaic.png"
_CACHE_TIF_NAME = "mosaic.tif"
_CACHE_VALID_MASK_NAME = "valid_mask.tif"
_CACHE_METADATA_NAME = "metadata.json"
_CACHE_LOCK_TIMEOUT_SEC = 30.0
_CACHE_LOCK_POLL_INTERVAL_SEC = 0.05


class MapboxCurrentImageryError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def build_mapbox_satellite_tile_url(
    *,
    tileset: str,
    zoom: int,
    x: int,
    y: int,
    image_format: str,
    access_token: str,
) -> str:
    encoded_tileset = quote(tileset, safe=".")
    encoded_format = quote(image_format, safe="")
    return f"https://api.mapbox.com/v4/{encoded_tileset}/{zoom}/{x}/{y}.{encoded_format}?access_token={access_token}"


def build_mapbox_current_cache_key(
    *,
    bbox: dict[str, float],
    zoom: int,
    tileset: str,
    image_format: str,
    tile_range: tuple[int, int, int, int] | None = None,
) -> str:
    resolved_tile_range = tile_range or tile_range_for_bbox(bbox, zoom)
    payload = {
        "version": _CACHE_VERSION,
        "provider": MAPBOX_PROVIDER,
        "source_type": MAPBOX_SOURCE_TYPE,
        "source_id": tileset,
        "zoom": zoom,
        "format": image_format,
        "tile_range": list(resolved_tile_range),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]
    return f"mapbox-current-{digest}"


def _cache_paths(cache_dir: Path) -> tuple[Path, Path, Path, Path]:
    return (
        cache_dir / _CACHE_PNG_NAME,
        cache_dir / _CACHE_TIF_NAME,
        cache_dir / _CACHE_VALID_MASK_NAME,
        cache_dir / _CACHE_METADATA_NAME,
    )


def _cache_block_size(size: int) -> int:
    if size >= 512:
        return 512
    if size >= 256:
        return 256
    return max(16, ((size + 15) // 16) * 16)


@contextmanager
def _acquire_cache_lock(cache_dir: Path):
    lock_dir = cache_dir.with_name(f"{cache_dir.name}.lock")
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_SEC
    while True:
        try:
            lock_dir.mkdir(parents=False)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Mapbox mosaic cache lock {lock_dir}.")
            time.sleep(_CACHE_LOCK_POLL_INTERVAL_SEC)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _valid_cache(cache_dir: Path, metadata: dict[str, object], *, cache_key: str, width: int, height: int) -> bool:
    png_path, tif_path, valid_mask_path, _metadata_path = _cache_paths(cache_dir)
    return (
        png_path.exists()
        and tif_path.exists()
        and valid_mask_path.exists()
        and metadata.get("version") == _CACHE_VERSION
        and metadata.get("cache_key") == cache_key
        and metadata.get("provider") == MAPBOX_PROVIDER
        and metadata.get("source_id") == MAPBOX_SOURCE_ID
        and metadata.get("width") == width
        and metadata.get("height") == height
    )


def _read_cache_metadata(cache_dir: Path, *, cache_key: str, width: int, height: int) -> dict[str, object] | None:
    metadata_path = cache_dir / _CACHE_METADATA_NAME
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata if _valid_cache(cache_dir, metadata, cache_key=cache_key, width=width, height=height) else None


def _write_cache(
    *,
    staging_dir: Path,
    canvas: Image.Image,
    valid_mask: np.ndarray,
    transform,
    metadata: dict[str, object],
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    png_path, tif_path, valid_mask_path, metadata_path = _cache_paths(staging_dir)
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


def _download_mapbox_tile(url: str, timeout_seconds: int) -> bytes:
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise MapboxCurrentImageryError(f"Mapbox tile download failed: {type(exc).__name__}") from None
    if response.status_code >= 400:
        raise MapboxCurrentImageryError(f"Mapbox tile download failed with HTTP {response.status_code}.")
    return response.content


def _result_from_cache(
    *,
    cache_dir: Path,
    cache_key: str,
    zoom: int,
    tile_count: int,
    tile_range: tuple[int, int, int, int],
    bounds_3857: tuple[float, float, float, float],
    metadata: dict[str, object],
) -> MosaicResult:
    png_path, tif_path, valid_mask_path, _metadata_path = _cache_paths(cache_dir)
    return MosaicResult(
        identifier=MAPBOX_SOURCE_ID,
        release_date="current_basemap",
        zoom=zoom,
        tile_count=tile_count,
        available_tile_count=tile_count,
        missing_tile_count=0,
        tile_range=tile_range,
        bounds_3857=bounds_3857,
        png_path=png_path,
        geotiff_path=tif_path,
        valid_mask_path=valid_mask_path,
        shared_cache_dir=cache_dir,
        cache_key=cache_key,
        materialized_in_request_dir=False,
        provider=MAPBOX_PROVIDER,
        source_type=MAPBOX_SOURCE_TYPE,
        source_id=MAPBOX_SOURCE_ID,
        effective_date="current_basemap",
        dominant_src_date=None,
        capture_date_known=False,
        attribution=MAPBOX_ATTRIBUTION,
        metadata=metadata,
    )


def download_mapbox_current_mosaic(
    bbox: dict[str, float],
    *,
    settings: Settings,
    zoom: int | None = None,
) -> MosaicResult:
    if not settings.mapbox_current_imagery_enabled:
        raise MapboxCurrentImageryError("Mapbox current imagery is disabled.")
    if not settings.mapbox_access_token:
        raise MapboxCurrentImageryError("Mapbox current imagery is enabled but MAPBOX_ACCESS_TOKEN is not configured.")

    resolved_zoom = min(zoom or settings.mapbox_current_imagery_default_zoom, settings.mapbox_current_imagery_max_zoom)
    tile_range = tile_range_for_bbox(bbox, resolved_zoom)
    x_min, x_max, y_min, y_max = tile_range
    tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
    if tile_count > settings.mapbox_current_imagery_max_tiles:
        raise MapboxCurrentImageryError(
            f"AOI would download {tile_count} Mapbox tiles at z={resolved_zoom}, exceeding the limit of "
            f"{settings.mapbox_current_imagery_max_tiles}."
        )

    width = (x_max - x_min + 1) * 256
    height = (y_max - y_min + 1) * 256
    left, _, _, top = tile_bounds_3857(x_min, y_min, resolved_zoom)
    _, bottom, right, _ = tile_bounds_3857(x_max, y_max, resolved_zoom)
    bounds_3857 = (left, bottom, right, top)
    cache_key = build_mapbox_current_cache_key(
        bbox=bbox,
        zoom=resolved_zoom,
        tileset=settings.mapbox_satellite_tileset,
        image_format=settings.mapbox_current_imagery_format,
        tile_range=tile_range,
    )
    cache_dir = settings.mapbox_current_imagery_cache_dir / cache_key

    with _acquire_cache_lock(cache_dir):
        cached_metadata = _read_cache_metadata(cache_dir, cache_key=cache_key, width=width, height=height)
        if cached_metadata is not None:
            metadata = dict(cached_metadata)
            metadata["cache_hit"] = True
            return _result_from_cache(
                cache_dir=cache_dir,
                cache_key=cache_key,
                zoom=resolved_zoom,
                tile_count=tile_count,
                tile_range=tile_range,
                bounds_3857=bounds_3857,
                metadata=metadata,
            )

        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

        canvas = Image.new("RGB", (width, height))
        valid_mask = np.zeros((height, width), dtype=np.uint8)
        jobs = [
            (
                x,
                y,
                build_mapbox_satellite_tile_url(
                    tileset=settings.mapbox_satellite_tileset,
                    zoom=resolved_zoom,
                    x=x,
                    y=y,
                    image_format=settings.mapbox_current_imagery_format,
                    access_token=settings.mapbox_access_token,
                ),
            )
            for y in range(y_min, y_max + 1)
            for x in range(x_min, x_max + 1)
        ]

        with ThreadPoolExecutor(max_workers=settings.download_workers) as executor:
            future_map = {
                executor.submit(_download_mapbox_tile, tile_url, settings.mapbox_current_imagery_timeout_seconds): (x, y)
                for x, y, tile_url in jobs
            }
            for future in as_completed(future_map):
                x, y = future_map[future]
                tile = Image.open(io.BytesIO(future.result())).convert("RGB")
                canvas.paste(tile, ((x - x_min) * 256, (y - y_min) * 256))
                valid_mask[(y - y_min) * 256 : (y - y_min + 1) * 256, (x - x_min) * 256 : (x - x_min + 1) * 256] = 1

        transform = from_bounds(*bounds_3857, width=width, height=height)
        metadata = {
            "version": _CACHE_VERSION,
            "cache_key": cache_key,
            "provider": MAPBOX_PROVIDER,
            "source_type": MAPBOX_SOURCE_TYPE,
            "source_id": MAPBOX_SOURCE_ID,
            "effective_date": "current_basemap",
            "dominant_src_date": None,
            "capture_date_known": False,
            "attribution_required": True,
            "attribution": MAPBOX_ATTRIBUTION,
            "zoom": resolved_zoom,
            "tile_range": list(tile_range),
            "tile_count": tile_count,
            "bounds_3857": list(bounds_3857),
            "width": width,
            "height": height,
            "format": settings.mapbox_current_imagery_format,
            "cache_hit": False,
        }
        staging_dir = Path(tempfile.mkdtemp(prefix=f"{cache_key}-", dir=str(settings.mapbox_current_imagery_cache_dir)))
        try:
            _write_cache(staging_dir=staging_dir, canvas=canvas, valid_mask=valid_mask, transform=transform, metadata=metadata)
            try:
                staging_dir.replace(cache_dir)
            except FileExistsError:
                shutil.rmtree(staging_dir, ignore_errors=True)
            except OSError:
                if cache_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                else:
                    raise
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return _result_from_cache(
        cache_dir=cache_dir,
        cache_key=cache_key,
        zoom=resolved_zoom,
        tile_count=tile_count,
        tile_range=tile_range,
        bounds_3857=bounds_3857,
        metadata=metadata,
    )
