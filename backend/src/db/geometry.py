from __future__ import annotations

import logging
from typing import Any

from geoalchemy2.elements import WKTElement
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


logger = logging.getLogger(__name__)


def _polygonal_parts(geometry: BaseGeometry) -> list[BaseGeometry]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return [geometry]
    if isinstance(geometry, GeometryCollection):
        parts: list[BaseGeometry] = []
        for item in geometry.geoms:
            parts.extend(_polygonal_parts(item))
        return parts
    return []


def polygonal_geojson_to_geometry(payload: dict[str, Any] | None) -> BaseGeometry | None:
    if not payload:
        return None

    try:
        payload_type = payload.get("type")
        candidates: list[dict[str, Any]] = []
        if payload_type == "FeatureCollection":
            for feature in payload.get("features") or []:
                if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict):
                    candidates.append(feature["geometry"])
        elif payload_type == "Feature" and isinstance(payload.get("geometry"), dict):
            candidates.append(payload["geometry"])
        else:
            candidates.append(payload)

        geometries = []
        for candidate in candidates:
            geometry = shape(candidate).buffer(0)
            geometries.extend(_polygonal_parts(geometry))
        if not geometries:
            return None
        geometry = unary_union(geometries).buffer(0)
        if isinstance(geometry, Polygon):
            return MultiPolygon([geometry])
        return geometry
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not convert GeoJSON to PostGIS geometry: %s", exc)
        return None


def geojson_to_wkt_element(payload: dict[str, Any] | None, *, srid: int = 4326) -> WKTElement | None:
    geometry = polygonal_geojson_to_geometry(payload)
    if geometry is None or geometry.is_empty:
        return None
    return WKTElement(geometry.wkt, srid=srid)
