from __future__ import annotations

from typing import Any

from pyproj import Geod, Transformer
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import transform as shapely_transform


GEOD = Geod(ellps="WGS84")


def _round_geometry_coordinates(value: Any, precision: int = 8) -> Any:
    if isinstance(value, float):
        return round(value, precision)
    if isinstance(value, list):
        return [_round_geometry_coordinates(item, precision) for item in value]
    if isinstance(value, tuple):
        return tuple(_round_geometry_coordinates(item, precision) for item in value)
    if isinstance(value, dict):
        return {key: _round_geometry_coordinates(item, precision) for key, item in value.items()}
    return value


def normalize_polygon(polygon: Polygon) -> Polygon:
    return orient(polygon.buffer(0), sign=1.0)


def normalize_geometry(geometry: BaseGeometry) -> BaseGeometry:
    repaired = geometry.buffer(0)
    if repaired.is_empty:
        raise ValueError("AOI became empty after geometry repair.")
    if isinstance(repaired, Polygon):
        return normalize_polygon(repaired)
    if isinstance(repaired, MultiPolygon):
        polygons = [normalize_polygon(part) for part in repaired.geoms if not part.is_empty]
        polygons.sort(key=lambda geom: (round(geom.centroid.x, 8), round(geom.centroid.y, 8), -geom.area))
        return MultiPolygon(polygons)
    raise ValueError("AOI must be a Polygon or MultiPolygon.")


def parse_aoi_geometry(aoi_geojson: dict[str, Any]) -> BaseGeometry:
    geometry = shape(aoi_geojson)
    if geometry.is_empty:
        raise ValueError("AOI geometry is empty.")
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("AOI must be a GeoJSON Polygon or MultiPolygon.")
    return normalize_geometry(geometry)


def normalized_aoi_geojson(aoi_geojson: dict[str, Any]) -> dict[str, Any]:
    geometry = parse_aoi_geometry(aoi_geojson)
    return _round_geometry_coordinates(mapping(geometry))


def geodesic_area_m2(geometry: BaseGeometry) -> float:
    return abs(float(GEOD.geometry_area_perimeter(geometry)[0]))


def bounds_dict(geometry: BaseGeometry) -> dict[str, float]:
    west, south, east, north = geometry.bounds
    return {"west": west, "south": south, "east": east, "north": north}


def centroid_lonlat(geometry: BaseGeometry) -> tuple[float, float]:
    centroid = geometry.centroid
    return float(centroid.x), float(centroid.y)


def utm_epsg_from_lonlat(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) // 6.0) + 1
    return 32600 + zone if lat >= 0 else 32700 + zone


def reproject_geometry(geometry: BaseGeometry, from_crs: str, to_crs: str) -> BaseGeometry:
    transformer = Transformer.from_crs(from_crs, to_crs, always_xy=True)
    return shapely_transform(transformer.transform, geometry)
