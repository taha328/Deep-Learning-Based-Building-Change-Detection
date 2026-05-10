from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

import numpy as np
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform


TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


@dataclass(frozen=True)
class PatchWindow:
    y0: int
    y1: int
    x0: int
    x1: int


def lonlat_to_tile_fraction(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = ((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0) * n
    return x, y


def tile_range_for_bbox(bbox: dict[str, float], zoom: int) -> tuple[int, int, int, int]:
    x0f, y1f = lonlat_to_tile_fraction(bbox["west"], bbox["south"], zoom)
    x1f, y0f = lonlat_to_tile_fraction(bbox["east"], bbox["north"], zoom)
    x_min = math.floor(min(x0f, x1f))
    x_max = math.floor(max(x0f, x1f))
    y_min = math.floor(min(y0f, y1f))
    y_max = math.floor(max(y0f, y1f))
    return x_min, x_max, y_min, y_max


def tile_bounds_3857(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    n = 2**zoom
    lon_left = x / n * 360.0 - 180.0
    lon_right = (x + 1) / n * 360.0 - 180.0
    lat_top = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_bottom = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    minx, miny = TO_3857.transform(lon_left, lat_bottom)
    maxx, maxy = TO_3857.transform(lon_right, lat_top)
    return minx, miny, maxx, maxy


def scene_tile_count(bbox: dict[str, float], zoom: int) -> int:
    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
    return (x_max - x_min + 1) * (y_max - y_min + 1)


def intersecting_tiles_for_aoi(
    aoi_geojson: dict[str, object] | None,
    *,
    bbox: dict[str, float],
    zoom: int,
) -> tuple[frozenset[tuple[int, int]] | None, int]:
    """Return AOI-intersecting xyz tiles and bbox candidate count.

    Returns (None, bbox_count) when AOI geometry cannot be evaluated safely, so callers can
    fall back to bbox-only selection.
    """
    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
    bbox_count = (x_max - x_min + 1) * (y_max - y_min + 1)
    if not aoi_geojson:
        return None, bbox_count
    try:
        aoi_geom = shape(aoi_geojson).buffer(0)
        if aoi_geom.is_empty:
            return frozenset(), bbox_count
        aoi_3857: BaseGeometry = shapely_transform(TO_3857.transform, aoi_geom)
    except Exception:
        return None, bbox_count

    selected: set[tuple[int, int]] = set()
    for y in range(y_min, y_max + 1):
        for x in range(x_min, x_max + 1):
            tile_poly = box(*tile_bounds_3857(x, y, zoom))
            if tile_poly.intersects(aoi_3857):
                selected.add((x, y))
    return frozenset(selected), bbox_count


def pixel_size_m_at_tile(x: int, y: int, zoom: int) -> tuple[float, float]:
    minx, miny, maxx, maxy = tile_bounds_3857(x, y, zoom)
    return (abs(maxx - minx) / 256.0, abs(maxy - miny) / 256.0)


def sliding_positions(length: int, patch_size: int, stride: int) -> list[int]:
    if length <= patch_size:
        return [0]
    positions = list(range(0, length - patch_size + 1, stride))
    if positions[-1] != length - patch_size:
        positions.append(length - patch_size)
    return positions


def pad_patch_rgb(patch: np.ndarray, patch_size: int) -> np.ndarray:
    if patch.shape[0] == patch_size and patch.shape[1] == patch_size:
        return patch
    padded = np.zeros((patch_size, patch_size, 3), dtype=np.uint8)
    padded[: patch.shape[0], : patch.shape[1], :] = patch
    return padded


def estimate_patch_count(height: int, width: int, patch_size: int, stride: int) -> int:
    return len(sliding_positions(height, patch_size, stride)) * len(
        sliding_positions(width, patch_size, stride)
    )


def iter_patch_windows(height: int, width: int, patch_size: int, stride: int) -> Iterator[PatchWindow]:
    for y0 in sliding_positions(height, patch_size, stride):
        for x0 in sliding_positions(width, patch_size, stride):
            y1 = min(y0 + patch_size, height)
            x1 = min(x0 + patch_size, width)
            yield PatchWindow(y0=y0, y1=y1, x0=x0, x1=x1)
