from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import logging
import tempfile
import time
from urllib.parse import quote
import warnings
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
import rasterio
from affine import Affine
from rasterio.errors import NotGeoreferencedWarning
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import transform_bounds
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

from src.domain.raster_write_options import large_geotiff_creation_options, validate_geotiff_file
from src.domain.reference_imagery_cache import (
    append_reference_imagery_materialization,
    build_aoi_hash,
    build_reference_imagery_cache_key_payload,
    build_reference_imagery_cache_metadata,
    build_reference_imagery_key,
    materialize_reference_imagery_cog,
    read_reference_imagery_cache_metadata,
    reference_imagery_cache_cog_path,
    reference_imagery_cache_metadata_path,
    valid_existing_canonical_cog,
    write_reference_imagery_cache_metadata,
)
from src.schemas import TemporalReferenceImagery
from src.utils.raster import rasterize_aoi_mask_like

if TYPE_CHECKING:
    from src.config import Settings


WEB_MERCATOR_CRS = "EPSG:3857"
WGS84_CRS = "EPSG:4326"
WEB_MERCATOR_HALF_WORLD = 20_037_508.342789244
WEB_MERCATOR_INITIAL_RESOLUTION = 156_543.03392804097
DEFAULT_TILE_SIZE = 256
DEFAULT_MIN_ZOOM = 0
DEFAULT_MAX_ZOOM = 22
REFERENCE_COG_FORMAT_VERSION = 4
REFERENCE_TILE_RENDERER_VERSION = 3
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemporalReferenceSource:
    image_path: str | None
    image_png_data_url: str | None
    raster_bounds_wgs84: list[float] | None
    source_raster_path: str | None
    valid_mask_path: str | None = None
    aoi_geojson: dict[str, object] | None = None


@dataclass(frozen=True)
class TemporalReferenceCogInfo:
    cog_path: Path
    bounds_wgs84: list[float] | None
    minzoom: int
    maxzoom: int
    tile_size: int
    cog_mtime: float | None = None
    cog_size: int | None = None
    cog_crs: str | None = None
    cog_transform: tuple[float, ...] | None = None
    cog_bounds: tuple[float, float, float, float] | None = None
    cog_width: int | None = None
    cog_height: int | None = None
    is_tiled: bool | None = None
    has_overviews: bool | None = None
    has_alpha_band: bool | None = None
    has_internal_mask: bool | None = None


@dataclass(frozen=True)
class _ReferenceImageryMetadataCacheEntry:
    info: TemporalReferenceCogInfo
    cog_mtime: float
    cog_size: int


@dataclass(frozen=True)
class _ReferenceCogDatasetMetadata:
    bounds_wgs84: list[float]
    minzoom: int
    maxzoom: int
    crs: str
    transform: tuple[float, ...]
    bounds: tuple[float, float, float, float]
    width: int
    height: int
    is_tiled: bool
    has_overviews: bool
    has_alpha_band: bool
    has_internal_mask: bool
    format_version: int | None
    valid_mask_mtime_ns: int | None


@dataclass(frozen=True)
class _TilejsonPayloadCacheEntry:
    payload: dict[str, object]
    cog_mtime: float
    cog_size: int


@dataclass(frozen=True)
class ReferenceTileRenderResult:
    content: bytes
    cache_hit: bool
    timings_ms: dict[str, float]
    warning_count: int
    dataset_band_count: int = 0
    dataset_has_alpha_band: bool = False
    dataset_has_internal_mask: bool = False
    tile_has_mask: bool = False
    output_png_has_alpha: bool = False
    transparent_pixel_count: int = 0
    opaque_pixel_count: int = 0
    alpha_source: str | None = None


@dataclass(frozen=True)
class ReferenceTileCacheResult:
    cache_path: Path
    cache_hit: bool
    render_result: ReferenceTileRenderResult | None = None
    cache_write_succeeded: bool = False
    cache_write_error: str | None = None


_REFERENCE_IMAGERY_METADATA_CACHE: dict[tuple[str, str, str, int, int], _ReferenceImageryMetadataCacheEntry] = {}
_TILEJSON_PAYLOAD_CACHE: dict[tuple[str, str, str, str, str], _TilejsonPayloadCacheEntry] = {}
_TILE_RESPONSE_CACHE: OrderedDict[tuple[object, ...], tuple[bytes, dict[str, object]]] = OrderedDict()
_TILE_RESPONSE_CACHE_MAX_ENTRIES = 512


def _tile_bounds_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    tile_span = (2 * WEB_MERCATOR_HALF_WORLD) / (2**z)
    minx = -WEB_MERCATOR_HALF_WORLD + (x * tile_span)
    maxx = minx + tile_span
    maxy = WEB_MERCATOR_HALF_WORLD - (y * tile_span)
    miny = maxy - tile_span
    return minx, miny, maxx, maxy


def _overview_factors(width: int, height: int) -> list[int]:
    smallest_dimension = min(width, height)
    factors: list[int] = []
    factor = 2
    while smallest_dimension / factor >= DEFAULT_TILE_SIZE:
        factors.append(factor)
        factor *= 2
    return factors


def _is_identity_transform(transform) -> bool:  # noqa: ANN001
    return transform == Affine.identity()


def _compute_zoom_range_from_bounds(
    *,
    bounds,
    crs,
    width: int,
    height: int,
) -> tuple[int, int]:
    if crs is None or width <= 0 or height <= 0:
        return DEFAULT_MIN_ZOOM, DEFAULT_MAX_ZOOM
    left, bottom, right, top = transform_bounds(crs, WEB_MERCATOR_CRS, *bounds, densify_pts=21)
    width_m = max(abs(right - left), 1.0)
    height_m = max(abs(top - bottom), 1.0)
    meters_per_pixel = max(width_m / max(width, 1), height_m / max(height, 1))
    estimated_max = int(round(math.log2(WEB_MERCATOR_INITIAL_RESOLUTION / max(meters_per_pixel, 1e-9))))
    return DEFAULT_MIN_ZOOM, max(DEFAULT_MIN_ZOOM, min(DEFAULT_MAX_ZOOM, estimated_max))


def _inspect_reference_cog(cog_path: Path) -> _ReferenceCogDatasetMetadata:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", NotGeoreferencedWarning)
        with rasterio.open(cog_path) as src:
            if any(issubclass(item.category, NotGeoreferencedWarning) for item in caught):
                raise ValueError(f"Reference imagery COG is not georeferenced: {cog_path}")
            if src.crs is None:
                raise ValueError(f"Reference imagery COG has no CRS: {cog_path}")
            if _is_identity_transform(src.transform):
                raise ValueError(f"Reference imagery COG has identity transform: {cog_path}")
            if src.width <= 0 or src.height <= 0:
                raise ValueError(f"Reference imagery COG has invalid dimensions: {cog_path}")
            bounds = src.bounds
            if not all(math.isfinite(value) for value in bounds):
                raise ValueError(f"Reference imagery COG has invalid bounds: {cog_path}")
            left, bottom, right, top = transform_bounds(src.crs, WGS84_CRS, *bounds, densify_pts=21)
            minzoom, maxzoom = _compute_zoom_range_from_bounds(
                bounds=bounds,
                crs=src.crs,
                width=src.width,
                height=src.height,
            )
            has_overviews = any(bool(src.overviews(index)) for index in range(1, src.count + 1))
            colorinterp = tuple(item.name for item in src.colorinterp)
            tags = src.tags(ns="building_change")
            format_version_raw = tags.get("reference_cog_format_version")
            valid_mask_mtime_raw = tags.get("valid_mask_mtime_ns")
            return _ReferenceCogDatasetMetadata(
                bounds_wgs84=[float(left), float(bottom), float(right), float(top)],
                minzoom=minzoom,
                maxzoom=maxzoom,
                crs=str(src.crs),
                transform=tuple(src.transform),
                bounds=(float(bounds.left), float(bounds.bottom), float(bounds.right), float(bounds.top)),
                width=src.width,
                height=src.height,
                is_tiled=bool(src.profile.get("tiled")),
                has_overviews=has_overviews,
                has_alpha_band="alpha" in colorinterp,
                has_internal_mask=any("per_dataset" in [flag.name for flag in flags] for flags in src.mask_flag_enums),
                format_version=int(format_version_raw) if format_version_raw and format_version_raw.isdigit() else None,
                valid_mask_mtime_ns=int(valid_mask_mtime_raw) if valid_mask_mtime_raw and valid_mask_mtime_raw.isdigit() else None,
            )


def _compute_bounds_wgs84(raster_path: Path) -> list[float] | None:
    try:
        with rasterio.open(raster_path) as src:
            if src.crs is None:
                return None
            if src.crs.to_string() == WGS84_CRS:
                return [
                    float(src.bounds.left),
                    float(src.bounds.bottom),
                    float(src.bounds.right),
                    float(src.bounds.top),
                ]
            left, bottom, right, top = transform_bounds(src.crs, WGS84_CRS, *src.bounds, densify_pts=21)
            return [float(left), float(bottom), float(right), float(top)]
    except Exception:
        return None


def _compute_bounds_3857(raster_path: Path) -> list[float] | None:
    try:
        with rasterio.open(raster_path) as src:
            if src.crs is None:
                return None
            if src.crs.to_string() == WEB_MERCATOR_CRS:
                return [
                    float(src.bounds.left),
                    float(src.bounds.bottom),
                    float(src.bounds.right),
                    float(src.bounds.top),
                ]
            left, bottom, right, top = transform_bounds(src.crs, WEB_MERCATOR_CRS, *src.bounds, densify_pts=21)
            return [float(left), float(bottom), float(right), float(top)]
    except Exception:
        return None


def _compute_zoom_range(raster_path: Path) -> tuple[int, int]:
    try:
        with rasterio.open(raster_path) as src:
            if src.crs is None or src.width <= 0 or src.height <= 0:
                return DEFAULT_MIN_ZOOM, DEFAULT_MAX_ZOOM
            left, bottom, right, top = transform_bounds(src.crs, WEB_MERCATOR_CRS, *src.bounds, densify_pts=21)
            width_m = max(abs(right - left), 1.0)
            height_m = max(abs(top - bottom), 1.0)
            meters_per_pixel = max(width_m / max(src.width, 1), height_m / max(src.height, 1))
            estimated_max = int(round(math.log2(WEB_MERCATOR_INITIAL_RESOLUTION / max(meters_per_pixel, 1e-9))))
            return DEFAULT_MIN_ZOOM, max(DEFAULT_MIN_ZOOM, min(DEFAULT_MAX_ZOOM, estimated_max))
    except Exception:
        return DEFAULT_MIN_ZOOM, DEFAULT_MAX_ZOOM


def _file_url(path: Path) -> str:
    return f"/api/files?path={quote(str(path), safe='')}"


def _reference_valid_mask_path_from_source(source_raster_path: Path | None) -> Path | None:
    if source_raster_path is None:
        return None
    name = source_raster_path.name
    if name == "t1_wayback_rgb.tif":
        candidates = list(source_raster_path.parent.glob("t1_*_valid_mask.tif"))
        if candidates:
            return sorted(candidates)[0]
    if name == "t2_wayback_rgb.tif":
        candidates = list(source_raster_path.parent.glob("t2_*_valid_mask.tif"))
        if candidates:
            return sorted(candidates)[0]
    if name == "source_wayback_rgb.tif":
        candidates = list(source_raster_path.parent.glob("source_*_valid_mask.tif"))
        if candidates:
            return sorted(candidates)[0]
    return None


def _tilejson_route(project_id: str, release_identifier: str) -> str:
    return (
        f"/api/temporal-projects/{quote(project_id, safe='')}"
        f"/milestones/{quote(release_identifier, safe='')}/reference/tilejson.json"
    )


def _tiles_route_template(project_id: str, release_identifier: str) -> str:
    return (
        f"/api/temporal-projects/{quote(project_id, safe='')}"
        f"/milestones/{quote(release_identifier, safe='')}/reference/tiles"
        "/{z}/{x}/{y}.png"
    )


def _ensure_cached_reference_imagery_cog(
    *,
    settings: "Settings",
    project_id: str,
    project_cog_path: Path,
    release_identifier: str,
    source_raster_path: Path,
    valid_mask_path: Path | None,
    aoi_geojson: dict[str, object] | None,
) -> tuple[Path, str, Path, str]:
    aoi_hash = build_aoi_hash(aoi_geojson)
    key_payload = build_reference_imagery_cache_key_payload(
        provider="esri_wayback",
        release_identifier=release_identifier,
        release_num=None,
        tile_matrix_set=settings.tile_matrix_set,
        zoom=settings.zoom,
        tile_range=None,
        bounds_3857=_compute_bounds_3857(source_raster_path),
        source_raster_path=source_raster_path,
        valid_mask_path=valid_mask_path,
        aoi_hash=aoi_hash,
        reference_cog_format_version=REFERENCE_COG_FORMAT_VERSION,
    )
    reference_imagery_key = build_reference_imagery_key(key_payload)
    if settings.reference_imagery_cache_dir is None:
        raise ValueError("Reference imagery cache is enabled but reference_imagery_cache_dir is not configured")
    canonical_cog_path = reference_imagery_cache_cog_path(settings.reference_imagery_cache_dir, reference_imagery_key)
    metadata_path = reference_imagery_cache_metadata_path(settings.reference_imagery_cache_dir, reference_imagery_key)
    canonical_reused = False

    if valid_existing_canonical_cog(canonical_cog_path, reference_imagery_key=reference_imagery_key):
        try:
            metadata = _inspect_reference_cog(canonical_cog_path)
            canonical_stat = canonical_cog_path.stat()
            source_stat = source_raster_path.stat()
            valid_mask_mtime_ns = valid_mask_path.stat().st_mtime_ns if valid_mask_path is not None and valid_mask_path.is_file() else None
            canonical_reused = (
                metadata.format_version == REFERENCE_COG_FORMAT_VERSION
                and metadata.has_alpha_band
                and canonical_stat.st_mtime_ns >= source_stat.st_mtime_ns
                and (valid_mask_mtime_ns is None or metadata.valid_mask_mtime_ns == valid_mask_mtime_ns)
            )
        except Exception:
            canonical_reused = False

    if not canonical_reused:
        ensure_reference_imagery_cog(
            source_raster_path,
            canonical_cog_path,
            valid_mask_path=valid_mask_path,
            aoi_geojson=aoi_geojson,
            project_id=project_id,
            release_identifier=release_identifier,
        )
        logger.info(
            "REFERENCE_IMAGERY_CACHE_CREATED projectId=%s releaseIdentifier=%s referenceImageryKey=%s canonicalCogPath=%s",
            project_id,
            release_identifier,
            reference_imagery_key,
            canonical_cog_path,
        )
    else:
        logger.info(
            "REFERENCE_IMAGERY_CACHE_HIT projectId=%s releaseIdentifier=%s referenceImageryKey=%s canonicalCogPath=%s",
            project_id,
            release_identifier,
            reference_imagery_key,
            canonical_cog_path,
        )

    materialization = materialize_reference_imagery_cog(
        canonical_cog_path=canonical_cog_path,
        project_cog_path=project_cog_path,
        mode=settings.reference_imagery_materialization,
    )
    existing_metadata = read_reference_imagery_cache_metadata(metadata_path)
    metadata = build_reference_imagery_cache_metadata(
        reference_imagery_key=reference_imagery_key,
        key_payload=key_payload,
        canonical_cog_path=canonical_cog_path,
        existing_metadata=existing_metadata,
    )
    append_reference_imagery_materialization(
        metadata,
        project_id=project_id,
        release_identifier=release_identifier,
        project_cog_path=project_cog_path,
        method=str(materialization["method"]),
    )
    write_reference_imagery_cache_metadata(metadata_path, metadata)
    logger.info(
        "REFERENCE_IMAGERY_CACHE_MATERIALIZED projectId=%s releaseIdentifier=%s referenceImageryKey=%s canonicalCogPath=%s projectCogPath=%s method=%s",
        project_id,
        release_identifier,
        reference_imagery_key,
        canonical_cog_path,
        project_cog_path,
        materialization["method"],
    )
    return project_cog_path, reference_imagery_key, canonical_cog_path, str(materialization["method"])


def ensure_reference_imagery_cog(
    source_raster_path: Path,
    cog_path: Path,
    *,
    valid_mask_path: Path | None = None,
    aoi_geojson: dict[str, object] | None = None,
    project_id: str | None = None,
    release_identifier: str | None = None,
) -> Path:
    started_at = time.perf_counter()
    source_raster_path = source_raster_path.resolve()
    cog_path = cog_path.resolve()
    cog_path.parent.mkdir(parents=True, exist_ok=True)
    source_stat = source_raster_path.stat()
    valid_mask_mtime_ns = valid_mask_path.stat().st_mtime_ns if valid_mask_path is not None and valid_mask_path.is_file() else None
    if cog_path.exists():
        try:
            metadata = _inspect_reference_cog(cog_path)
            cog_stat = cog_path.stat()
            if (
                metadata.format_version == REFERENCE_COG_FORMAT_VERSION
                and metadata.has_alpha_band
                and cog_stat.st_mtime_ns >= source_stat.st_mtime_ns
                and (
                    valid_mask_mtime_ns is None
                    or metadata.valid_mask_mtime_ns == valid_mask_mtime_ns
                )
            ):
                logger.debug("COG_REUSED path=%s", cog_path)
                return cog_path
        except Exception:
            pass

    temp_path = cog_path.with_suffix(".tmp.tif")
    if temp_path.exists():
        temp_path.unlink()

    try:
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            with rasterio.open(source_raster_path) as src:
                if src.crs is None:
                    raise ValueError(f"Reference imagery source raster has no CRS: {source_raster_path}")
                if _is_identity_transform(src.transform):
                    raise ValueError(f"Reference imagery source raster has identity transform: {source_raster_path}")
                source_band_count = min(src.count, 3)
                if source_band_count < 3:
                    raise ValueError(f"Reference imagery source raster must have at least three RGB bands: {source_raster_path}")
                profile = src.profile.copy()
                cog_options, estimated_uncompressed_bytes = large_geotiff_creation_options(
                    width=src.width,
                    height=src.height,
                    band_count=4,
                    dtype=profile.get("dtype", src.dtypes[0]),
                    compression="DEFLATE",
                    predictor=2,
                    block_size=DEFAULT_TILE_SIZE,
                )
                logger.info(
                    "REFERENCE_COG_SIZE_ESTIMATE project_id=%s release_identifier=%s source_path=%s cog_path=%s "
                    "width=%s height=%s bands=4 dtype=%s compression=DEFLATE estimatedUncompressedBytes=%s",
                    project_id or "unknown",
                    release_identifier or "unknown",
                    source_raster_path,
                    cog_path,
                    src.width,
                    src.height,
                    profile.get("dtype", src.dtypes[0]),
                    estimated_uncompressed_bytes,
                )
                logger.info(
                    "REFERENCE_COG_GTIFF_OPTIONS project_id=%s release_identifier=%s cog_path=%s options=%s",
                    project_id or "unknown",
                    release_identifier or "unknown",
                    cog_path,
                    cog_options,
                )
                profile.update(
                    driver="GTiff",
                    count=4,
                    nodata=None,
                    interleave="pixel",
                    **cog_options,
                )
                try:
                    with rasterio.open(temp_path, "w", **profile) as dst:
                        aoi_mask = None
                        if aoi_geojson is not None:
                            aoi_mask = np.where(rasterize_aoi_mask_like(source_raster_path, aoi_geojson), 255, 0).astype(np.uint8)
                            if aoi_mask.shape != (src.height, src.width):
                                aoi_mask = None
                        valid_src = rasterio.open(valid_mask_path) if valid_mask_path is not None and valid_mask_path.is_file() else None
                        has_source_alpha = bool(src.count >= 4 and src.colorinterp and src.colorinterp[3] == ColorInterp.alpha)
                        has_source_internal_mask = any("per_dataset" in [flag.name for flag in flags] for flags in src.mask_flag_enums)
                        has_authoritative_mask = bool(has_source_alpha or has_source_internal_mask or valid_src is not None or aoi_mask is not None)
                        fallback_used = False
                        alpha_zero_count = 0
                        alpha_255_count = 0
                        try:
                            for _block_index, window in src.block_windows(1):
                                rgb = src.read([1, 2, 3], window=window)
                                dst.write(rgb, indexes=[1, 2, 3], window=window)
                                if src.count >= 4 and src.colorinterp and src.colorinterp[3] == ColorInterp.alpha:
                                    alpha = src.read(4, window=window).astype(np.uint8)
                                else:
                                    alpha = src.dataset_mask(window=window).astype(np.uint8)
                                if valid_src is not None:
                                    valid = valid_src.read(1, window=window)
                                    if valid.shape == alpha.shape:
                                        alpha = np.minimum(alpha, np.where(valid > 0, 255, 0).astype(np.uint8))
                                if aoi_mask is not None:
                                    row_start = int(window.row_off)
                                    row_stop = row_start + int(window.height)
                                    col_start = int(window.col_off)
                                    col_stop = col_start + int(window.width)
                                    alpha = np.minimum(alpha, aoi_mask[row_start:row_stop, col_start:col_stop])
                                if not has_authoritative_mask and not np.any(alpha == 0):
                                    fallback_alpha = np.where(np.any(rgb != 0, axis=0), 255, 0).astype(np.uint8)
                                    if np.any(fallback_alpha == 0):
                                        alpha = fallback_alpha
                                        fallback_used = True
                                alpha_zero_count += int((alpha == 0).sum())
                                alpha_255_count += int((alpha == 255).sum())
                                dst.write(alpha, 4, window=window)
                        finally:
                            if valid_src is not None:
                                valid_src.close()
                        dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                        if src.descriptions:
                            dst.descriptions = tuple(list(src.descriptions[:3]) + ["Alpha"])
                        if src.units:
                            dst.units = tuple(list(src.units[:3]) + [""])
                        dst.update_tags(
                            ns="building_change",
                            reference_cog_format_version=str(REFERENCE_COG_FORMAT_VERSION),
                            valid_mask_mtime_ns="" if valid_mask_mtime_ns is None else str(valid_mask_mtime_ns),
                        )
                        overview_factors = _overview_factors(dst.width, dst.height)
                        if overview_factors:
                            dst.build_overviews(overview_factors, Resampling.average)
                            dst.update_tags(ns="rio_overview", resampling="average")
                    validation = validate_geotiff_file(
                        temp_path,
                        expected_width=src.width,
                        expected_height=src.height,
                        min_band_count=4,
                    )
                    inspected = _inspect_reference_cog(temp_path)
                    if inspected.format_version != REFERENCE_COG_FORMAT_VERSION or not inspected.has_alpha_band:
                        raise ValueError(f"Reference imagery COG validation failed for {temp_path}")
                    logger.info(
                        "REFERENCE_COG_VALIDATE_DONE project_id=%s release_identifier=%s cog_path=%s temp_path=%s validation=%s",
                        project_id or "unknown",
                        release_identifier or "unknown",
                        cog_path,
                        temp_path,
                        validation,
                    )
                    logger.info(
                        "REFERENCE_COG_ALPHA_CONFIRMED project_id=%s release_identifier=%s cog_path=%s band_count=4 alpha_zero_count=%s alpha_255_count=%s duration_ms=%s",
                        project_id or "unknown",
                        release_identifier or "unknown",
                        cog_path,
                        alpha_zero_count,
                        alpha_255_count,
                        round((time.perf_counter() - started_at) * 1000, 2),
                    )
                    if fallback_used:
                        logger.info(
                            "REFERENCE_COG_MASK_FALLBACK_RGB_NONZERO_USED project_id=%s release_identifier=%s cog_path=%s band_count=4 alpha_zero_count=%s alpha_255_count=%s",
                            project_id or "unknown",
                            release_identifier or "unknown",
                            cog_path,
                            alpha_zero_count,
                            alpha_255_count,
                        )
                except Exception as exc:
                    logger.exception(
                        "REFERENCE_COG_WRITE_FAILED project_id=%s release_identifier=%s cog_path=%s temp_path=%s "
                        "error=%s hint=BigTIFF/large-raster-write-failed",
                        project_id or "unknown",
                        release_identifier or "unknown",
                        cog_path,
                        temp_path,
                        exc,
                    )
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                    raise RuntimeError(f"REFERENCE_COG BigTIFF/large raster writing failed for {cog_path}: {exc}") from exc
        os.replace(temp_path, cog_path)
        logger.info(
            "COG_CREATED path=%s durationMs=%s",
            cog_path,
            round((time.perf_counter() - started_at) * 1000, 2),
        )
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return cog_path


def build_temporal_reference_imagery(
    *,
    project_id: str,
    project_dir: Path,
    release_identifier: str,
    source: TemporalReferenceSource,
    settings: "Settings | None" = None,
) -> TemporalReferenceImagery | None:
    image_path = source.image_path
    image_png_data_url = source.image_png_data_url
    raster_bounds_wgs84 = source.raster_bounds_wgs84
    source_raster_path = Path(source.source_raster_path).resolve() if source.source_raster_path else None
    valid_mask_path = (
        Path(source.valid_mask_path).resolve()
        if source.valid_mask_path
        else _reference_valid_mask_path_from_source(source_raster_path)
    )

    if image_path is None and image_png_data_url is None and source_raster_path is None:
        return None

    cog_path: Path | None = None
    minzoom = DEFAULT_MIN_ZOOM
    maxzoom = DEFAULT_MAX_ZOOM
    if source_raster_path and source_raster_path.is_file():
        milestone_dir = project_dir / "milestones" / release_identifier
        target_cog_path = milestone_dir / "reference_imagery_cog.tif"
        cog_existed_before = target_cog_path.is_file()
        reference_imagery_key: str | None = None
        canonical_cog_path: Path | None = None
        materialization_method: str | None = None
        if settings is not None and settings.reference_imagery_cache_enabled:
            cog_path, reference_imagery_key, canonical_cog_path, materialization_method = _ensure_cached_reference_imagery_cog(
                settings=settings,
                project_id=project_id,
                project_cog_path=target_cog_path,
                release_identifier=release_identifier,
                source_raster_path=source_raster_path,
                valid_mask_path=valid_mask_path,
                aoi_geojson=source.aoi_geojson,
            )
        else:
            cog_path = ensure_reference_imagery_cog(
                source_raster_path,
                target_cog_path,
                valid_mask_path=valid_mask_path,
                aoi_geojson=source.aoi_geojson,
                project_id=project_id,
                release_identifier=release_identifier,
            )
        if not cog_existed_before:
            logger.info("COG_CREATED projectId=%s releaseIdentifier=%s path=%s", project_id, release_identifier, cog_path)
        raster_bounds_wgs84 = raster_bounds_wgs84 or _compute_bounds_wgs84(cog_path)
        minzoom, maxzoom = _compute_zoom_range(cog_path)
        if image_path is not None:
            image_png_data_url = None
    else:
        reference_imagery_key = None
        canonical_cog_path = None
        materialization_method = None

    storage_strategy = "raster_tiles" if cog_path else "image_overlay"
    return TemporalReferenceImagery(
        image_path=image_path,
        image_png_data_url=image_png_data_url,
        raster_bounds_wgs84=raster_bounds_wgs84,
        storage_strategy=storage_strategy,
        cog_path=str(cog_path) if cog_path else None,
        cog_url=_file_url(cog_path) if cog_path else None,
        tilejson_url=_tilejson_route(project_id, release_identifier) if cog_path else None,
        tiles_url_template=_tiles_route_template(project_id, release_identifier) if cog_path else None,
        minzoom=minzoom if cog_path else None,
        maxzoom=maxzoom if cog_path else None,
        tile_size=DEFAULT_TILE_SIZE if cog_path else None,
        reference_imagery_key=reference_imagery_key,
        canonical_cog_path=str(canonical_cog_path) if canonical_cog_path else None,
        materialization_method=materialization_method,
    )


def resolve_temporal_reference_cog(reference_imagery: TemporalReferenceImagery | None) -> TemporalReferenceCogInfo | None:
    if reference_imagery is None or not reference_imagery.cog_path:
        if reference_imagery is not None:
            logger.info("COG_MISSING reason=missing_cog_path")
        return None
    cog_path = Path(reference_imagery.cog_path).resolve()
    if not cog_path.is_file():
        logger.info("COG_MISSING path=%s", cog_path)
        return None
    return TemporalReferenceCogInfo(
        cog_path=cog_path,
        bounds_wgs84=reference_imagery.raster_bounds_wgs84,
        minzoom=reference_imagery.minzoom if reference_imagery.minzoom is not None else DEFAULT_MIN_ZOOM,
        maxzoom=reference_imagery.maxzoom if reference_imagery.maxzoom is not None else DEFAULT_MAX_ZOOM,
        tile_size=reference_imagery.tile_size if reference_imagery.tile_size is not None else DEFAULT_TILE_SIZE,
    )


def resolve_temporal_reference_cog_cached(
    *,
    project_id: str,
    release_identifier: str,
    reference_imagery: TemporalReferenceImagery | None,
) -> TemporalReferenceCogInfo | None:
    if reference_imagery is None or not reference_imagery.cog_path:
        logger.info("COG_MISSING projectId=%s releaseIdentifier=%s reason=missing_cog_path", project_id, release_identifier)
        return None

    cog_path = Path(reference_imagery.cog_path).resolve()
    if not cog_path.is_file():
        logger.info("COG_MISSING projectId=%s releaseIdentifier=%s path=%s", project_id, release_identifier, cog_path)
        return None

    stat = cog_path.stat()
    cache_key = (project_id, release_identifier, str(cog_path), stat.st_mtime_ns, stat.st_size)
    cached = _REFERENCE_IMAGERY_METADATA_CACHE.get(cache_key)
    if cached:
        logger.info(
            "REFERENCE_IMAGERY_METADATA_CACHE_HIT projectId=%s releaseIdentifier=%s cacheKey=%s",
            project_id,
            release_identifier,
            ":".join(str(part) for part in cache_key),
        )
        return cached.info

    logger.info(
        "REFERENCE_IMAGERY_METADATA_CACHE_MISS projectId=%s releaseIdentifier=%s cacheKey=%s",
        project_id,
        release_identifier,
        ":".join(str(part) for part in cache_key),
    )
    try:
        metadata = _inspect_reference_cog(cog_path)
    except ValueError as exc:
        logger.warning(
            "COG_MISSING projectId=%s releaseIdentifier=%s path=%s reason=invalid_cog detail=%s",
            project_id,
            release_identifier,
            cog_path,
            exc,
        )
        return None

    bounds_wgs84 = reference_imagery.raster_bounds_wgs84 or metadata.bounds_wgs84
    if reference_imagery.minzoom is not None and reference_imagery.maxzoom is not None:
        minzoom = reference_imagery.minzoom
        maxzoom = reference_imagery.maxzoom
    else:
        minzoom, maxzoom = metadata.minzoom, metadata.maxzoom
    info = TemporalReferenceCogInfo(
        cog_path=cog_path,
        bounds_wgs84=bounds_wgs84,
        minzoom=minzoom,
        maxzoom=maxzoom,
        tile_size=reference_imagery.tile_size if reference_imagery.tile_size is not None else DEFAULT_TILE_SIZE,
        cog_mtime=stat.st_mtime,
        cog_size=stat.st_size,
        cog_crs=metadata.crs,
        cog_transform=metadata.transform,
        cog_bounds=metadata.bounds,
        cog_width=metadata.width,
        cog_height=metadata.height,
        is_tiled=metadata.is_tiled,
            has_overviews=metadata.has_overviews,
            has_alpha_band=metadata.has_alpha_band,
            has_internal_mask=metadata.has_internal_mask,
        )
    _REFERENCE_IMAGERY_METADATA_CACHE[cache_key] = _ReferenceImageryMetadataCacheEntry(
        info=info,
        cog_mtime=stat.st_mtime,
        cog_size=stat.st_size,
    )
    return info


def clear_reference_tile_cache() -> None:
    _TILE_RESPONSE_CACHE.clear()


def clear_reference_tilejson_cache() -> None:
    _TILEJSON_PAYLOAD_CACHE.clear()


def _cache_tile_bytes(cache_key: tuple[object, ...], tile_bytes: bytes, metadata: dict[str, object]) -> None:
    _TILE_RESPONSE_CACHE[cache_key] = (tile_bytes, metadata)
    _TILE_RESPONSE_CACHE.move_to_end(cache_key)
    while len(_TILE_RESPONSE_CACHE) > _TILE_RESPONSE_CACHE_MAX_ENTRIES:
        _TILE_RESPONSE_CACHE.popitem(last=False)


def _transparent_tile_png(tile_size: int) -> bytes:
    image = Image.fromarray(np.zeros((tile_size, tile_size, 4), dtype=np.uint8))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@lru_cache(maxsize=128)
def _reference_cog_content_signature(cog_path: str, mtime_ns: int, size_bytes: int) -> str:
    path = Path(cog_path)
    sidecar_path = path.with_suffix(path.suffix + ".version.json")
    sidecar_key = {"mtime_ns": mtime_ns, "size_bytes": size_bytes}
    if sidecar_path.is_file():
        try:
            sidecar = json.loads(sidecar_path.read_text())
            if (
                int(sidecar.get("mtime_ns", -1)) == mtime_ns
                and int(sidecar.get("size_bytes", -1)) == size_bytes
                and isinstance(sidecar.get("content_signature"), str)
            ):
                return str(sidecar["content_signature"])
        except Exception:
            pass

    digest = hashlib.sha256()
    digest.update(str(mtime_ns).encode("ascii"))
    digest.update(str(size_bytes).encode("ascii"))
    sample_size = min(1024 * 1024, max(size_bytes, 0))
    offsets = [0]
    if size_bytes > sample_size:
        offsets.append(max(0, (size_bytes // 2) - (sample_size // 2)))
        offsets.append(max(0, size_bytes - sample_size))
    with path.open("rb") as handle:
        for offset in dict.fromkeys(offsets):
            handle.seek(offset)
            digest.update(handle.read(sample_size))
    try:
        metadata = _inspect_reference_cog(path)
        digest.update(str(metadata.format_version).encode("ascii"))
        digest.update(str(metadata.valid_mask_mtime_ns).encode("ascii"))
        digest.update(str(metadata.has_alpha_band).encode("ascii"))
        digest.update(str(metadata.has_internal_mask).encode("ascii"))
        digest.update(str(metadata.bounds).encode("utf-8"))
        digest.update(str(metadata.width).encode("ascii"))
        digest.update(str(metadata.height).encode("ascii"))
    except Exception:
        pass
    signature = digest.hexdigest()[:24]
    try:
        sidecar_path.write_text(json.dumps({**sidecar_key, "content_signature": signature}, indent=2))
    except OSError:
        pass
    return signature


def reference_imagery_version_token(cog_info: TemporalReferenceCogInfo) -> str:
    stat = cog_info.cog_path.stat()
    stat_mtime_ns = stat.st_mtime_ns
    stat_size = int(cog_info.cog_size) if cog_info.cog_size is not None else int(stat.st_size)
    signature = _reference_cog_content_signature(str(cog_info.cog_path), stat_mtime_ns, stat_size)
    return f"{stat_mtime_ns}-{stat_size}-{signature}-renderer{REFERENCE_TILE_RENDERER_VERSION}"


def _safe_cache_component(value: str) -> str:
    return quote(value, safe="")


def reference_tile_cache_path(
    cache_root: Path,
    *,
    project_id: str,
    release_identifier: str,
    cog_version: str,
    z: int,
    x: int,
    y: int,
) -> Path:
    return (
        cache_root
        / _safe_cache_component(project_id)
        / _safe_cache_component(release_identifier)
        / _safe_cache_component(cog_version)
        / str(z)
        / str(x)
        / f"{y}.png"
    )


def _write_tile_cache_atomic(cache_path: Path, tile_bytes: bytes) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".tmp",
            prefix=f".{cache_path.name}.",
            dir=cache_path.parent,
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(tile_bytes)
        os.replace(temp_name, cache_path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def ensure_reference_tile_png_cached_on_disk(
    *,
    cache_root: Path,
    project_id: str,
    release_identifier: str,
    cog_info: TemporalReferenceCogInfo,
    z: int,
    x: int,
    y: int,
) -> ReferenceTileCacheResult:
    cog_version = reference_imagery_version_token(cog_info)
    cache_path = reference_tile_cache_path(
        cache_root,
        project_id=project_id,
        release_identifier=release_identifier,
        cog_version=cog_version,
        z=z,
        x=x,
        y=y,
    )
    if cache_path.is_file():
        return ReferenceTileCacheResult(cache_path=cache_path, cache_hit=True)

    render_result = render_reference_tile_png_cached(
        project_id=project_id,
        release_identifier=release_identifier,
        cog_info=cog_info,
        z=z,
        x=x,
        y=y,
    )
    try:
        _write_tile_cache_atomic(cache_path, render_result.content)
    except OSError as exc:
        logger.warning(
            "TILE_CACHE_WRITE_FAILED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s cachePath=%s error=%s",
            project_id,
            release_identifier,
            z,
            x,
            y,
            cache_path,
            exc,
        )
        return ReferenceTileCacheResult(
            cache_path=cache_path,
            cache_hit=False,
            render_result=render_result,
            cache_write_succeeded=False,
            cache_write_error=f"{type(exc).__name__}: {exc}",
        )
    return ReferenceTileCacheResult(
        cache_path=cache_path,
        cache_hit=False,
        render_result=render_result,
        cache_write_succeeded=True,
    )


def _encode_png_with_optional_alpha(rgb_or_rgba: np.ndarray, alpha: np.ndarray | None) -> tuple[bytes, bool, int, int]:
    if rgb_or_rgba.ndim != 3:
        raise ValueError("Expected HWC image array for PNG encoding.")
    if alpha is not None:
        rgba = np.dstack([rgb_or_rgba[:, :, :3], alpha.astype(np.uint8)])
        output = BytesIO()
        Image.fromarray(rgba, mode="RGBA").save(output, format="PNG")
        transparent_count = int((alpha == 0).sum())
        opaque_count = int((alpha > 0).sum())
        return output.getvalue(), True, transparent_count, opaque_count
    output = BytesIO()
    Image.fromarray(rgb_or_rgba[:, :, :3], mode="RGB").save(output, format="PNG")
    return output.getvalue(), False, 0, int(rgb_or_rgba.shape[0] * rgb_or_rgba.shape[1])


def _fallback_black_padding_alpha(rgb: np.ndarray) -> np.ndarray:
    return np.where(np.any(rgb[:, :, :3] != 0, axis=2), 255, 0).astype(np.uint8)


def render_reference_tile_png_cached(
    *,
    project_id: str,
    release_identifier: str,
    cog_info: TemporalReferenceCogInfo,
    z: int,
    x: int,
    y: int,
) -> ReferenceTileRenderResult:
    total_started_at = time.perf_counter()
    stat = cog_info.cog_path.stat()
    cog_version = reference_imagery_version_token(cog_info)
    cache_key = (
        project_id,
        release_identifier,
        str(cog_info.cog_path),
        cog_version,
        z,
        x,
        y,
        cog_info.tile_size,
        "png",
    )
    cached = _TILE_RESPONSE_CACHE.get(cache_key)
    if cached is not None:
        _TILE_RESPONSE_CACHE.move_to_end(cache_key)
        cached_bytes, cached_meta = cached
        return ReferenceTileRenderResult(
            content=cached_bytes,
            cache_hit=True,
            timings_ms={
                "cog_open": 0.0,
                "window_calc": 0.0,
                "read": 0.0,
                "reproject": 0.0,
                "encode": 0.0,
                "total": round((time.perf_counter() - total_started_at) * 1000, 2),
            },
            warning_count=0,
            dataset_band_count=int(cached_meta.get("dataset_band_count", 0)),
            dataset_has_alpha_band=bool(cached_meta.get("dataset_has_alpha_band", False)),
            dataset_has_internal_mask=bool(cached_meta.get("dataset_has_internal_mask", False)),
            tile_has_mask=bool(cached_meta.get("tile_has_mask", False)),
            output_png_has_alpha=bool(cached_meta.get("output_png_has_alpha", False)),
            transparent_pixel_count=int(cached_meta.get("transparent_pixel_count", 0)),
            opaque_pixel_count=int(cached_meta.get("opaque_pixel_count", 0)),
            alpha_source=str(cached_meta.get("alpha_source")) if cached_meta.get("alpha_source") else None,
        )

    warning_count = 0
    dataset_band_count = 0
    dataset_has_alpha_band = False
    dataset_has_internal_mask = False
    tile_has_mask = False
    output_png_has_alpha = False
    transparent_pixel_count = 0
    opaque_pixel_count = 0
    alpha_source: str | None = None
    open_started_at = time.perf_counter()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", NotGeoreferencedWarning)
        try:
            with Reader(str(cog_info.cog_path)) as reader:
                open_ms = round((time.perf_counter() - open_started_at) * 1000, 2)
                read_started_at = time.perf_counter()
                image_data = reader.tile(x, y, z, tilesize=cog_info.tile_size)
                read_ms = round((time.perf_counter() - read_started_at) * 1000, 2)
                dataset_band_count = int(image_data.array.shape[0])
                dataset_has_alpha_band = dataset_band_count == 4
                tile_has_mask = image_data.mask is not None
                dataset_has_internal_mask = tile_has_mask and not dataset_has_alpha_band
                encode_started_at = time.perf_counter()
                rgb = np.transpose(image_data.array[:3], (1, 2, 0)).astype(np.uint8)
                alpha = None
                if dataset_has_alpha_band:
                    alpha = image_data.array[3].astype(np.uint8)
                    alpha_source = "dataset_alpha"
                elif image_data.mask is not None:
                    alpha = np.asarray(image_data.mask, dtype=np.uint8)
                    alpha_source = "dataset_mask"
                if alpha is None:
                    alpha = np.full((rgb.shape[0], rgb.shape[1]), 255, dtype=np.uint8)
                    alpha_source = "opaque_default"
                alpha_has_transparency = bool(np.any(alpha == 0))
                has_authoritative_alpha = bool(dataset_has_alpha_band or cog_info.has_internal_mask)
                if not alpha_has_transparency and not has_authoritative_alpha:
                    fallback_alpha = _fallback_black_padding_alpha(rgb)
                    fallback_transparent_count = int((fallback_alpha == 0).sum())
                    if fallback_transparent_count > 0:
                        alpha = fallback_alpha
                        alpha_source = "black_padding_without_mask"
                        tile_has_mask = True
                tile_bytes, output_png_has_alpha, transparent_pixel_count, opaque_pixel_count = _encode_png_with_optional_alpha(rgb, alpha)
                encode_ms = round((time.perf_counter() - encode_started_at) * 1000, 2)
        except TileOutsideBounds:
            open_ms = round((time.perf_counter() - open_started_at) * 1000, 2)
            read_ms = 0.0
            encode_started_at = time.perf_counter()
            tile_bytes = _transparent_tile_png(cog_info.tile_size)
            encode_ms = round((time.perf_counter() - encode_started_at) * 1000, 2)
            output_png_has_alpha = True
            transparent_pixel_count = cog_info.tile_size * cog_info.tile_size
            opaque_pixel_count = 0
            alpha_source = "outside_bounds"
        warning_count = sum(1 for item in caught if issubclass(item.category, NotGeoreferencedWarning))

    _cache_tile_bytes(
        cache_key,
        tile_bytes,
        {
            "dataset_band_count": dataset_band_count,
            "dataset_has_alpha_band": dataset_has_alpha_band,
            "dataset_has_internal_mask": dataset_has_internal_mask,
            "tile_has_mask": tile_has_mask,
            "output_png_has_alpha": output_png_has_alpha,
            "transparent_pixel_count": transparent_pixel_count,
            "opaque_pixel_count": opaque_pixel_count,
            "alpha_source": alpha_source,
        },
    )
    return ReferenceTileRenderResult(
        content=tile_bytes,
        cache_hit=False,
        timings_ms={
            "cog_open": open_ms,
            "window_calc": 0.0,
            "read": read_ms,
            "reproject": 0.0,
            "encode": encode_ms,
            "total": round((time.perf_counter() - total_started_at) * 1000, 2),
        },
        warning_count=warning_count,
        dataset_band_count=dataset_band_count,
        dataset_has_alpha_band=dataset_has_alpha_band,
        dataset_has_internal_mask=dataset_has_internal_mask,
        tile_has_mask=tile_has_mask,
        output_png_has_alpha=output_png_has_alpha,
        transparent_pixel_count=transparent_pixel_count,
        opaque_pixel_count=opaque_pixel_count,
        alpha_source=alpha_source,
    )


def build_reference_tilejson_payload(
    *,
    name: str,
    tiles_url: str,
    bounds_wgs84: list[float] | None,
    minzoom: int,
    maxzoom: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "tilejson": "3.0.0",
        "name": name,
        "version": "1.0.0",
        "scheme": "xyz",
        "tiles": [tiles_url],
        "minzoom": minzoom,
        "maxzoom": maxzoom,
    }
    if bounds_wgs84 is not None:
        payload["bounds"] = bounds_wgs84
    return payload


def build_reference_tilejson_payload_cached(
    *,
    project_id: str,
    release_identifier: str,
    cog_info: TemporalReferenceCogInfo,
    name: str,
    tiles_url: str,
) -> tuple[dict[str, object], bool]:
    stat = cog_info.cog_path.stat()
    cache_key = (
        project_id,
        release_identifier,
        str(cog_info.cog_path),
        reference_imagery_version_token(cog_info),
        tiles_url,
    )
    cached = _TILEJSON_PAYLOAD_CACHE.get(cache_key)
    if cached:
        return cached.payload, True

    payload = build_reference_tilejson_payload(
        name=name,
        tiles_url=tiles_url,
        bounds_wgs84=cog_info.bounds_wgs84,
        minzoom=cog_info.minzoom,
        maxzoom=cog_info.maxzoom,
    )
    _TILEJSON_PAYLOAD_CACHE[cache_key] = _TilejsonPayloadCacheEntry(
        payload=payload,
        cog_mtime=stat.st_mtime,
        cog_size=stat.st_size,
    )
    return payload, False


def render_reference_tile_png(cog_path: Path, z: int, x: int, y: int, *, tile_size: int = DEFAULT_TILE_SIZE) -> bytes:
    stat = cog_path.stat()
    cog_info = TemporalReferenceCogInfo(
        cog_path=cog_path,
        bounds_wgs84=None,
        minzoom=DEFAULT_MIN_ZOOM,
        maxzoom=DEFAULT_MAX_ZOOM,
        tile_size=tile_size,
        cog_mtime=stat.st_mtime,
        cog_size=stat.st_size,
    )
    return render_reference_tile_png_cached(
        project_id="direct",
        release_identifier=str(cog_path),
        cog_info=cog_info,
        z=z,
        x=x,
        y=y,
    ).content
