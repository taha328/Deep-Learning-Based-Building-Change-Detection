from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import shapes
from pyproj import Geod
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, split, unary_union

try:
    from shapely import concave_hull
except ImportError:  # pragma: no cover - fallback for older GEOS/Shapely stacks.
    concave_hull = None

from src.utils.geometry import centroid_lonlat, geodesic_area_m2, parse_aoi_geometry, reproject_geometry, utm_epsg_from_lonlat


GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class VectorizationContext:
    release_t1: str
    release_t2: str
    src_date_t1: str | None
    src_date_t2: str | None


@dataclass(frozen=True)
class SegmentationVectorizationContext:
    release: str
    src_date: str | None
    prompt: str | None = None


def empty_feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def feature_collection_records(feature_collection: dict[str, Any]) -> list[dict[str, Any]]:
    return [feature.get("properties", {}) for feature in feature_collection.get("features", [])]


def _vectorize_regions(
    mask: np.ndarray,
    reference_path: Path,
    context: VectorizationContext,
    *,
    id_field: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    features_out: list[dict[str, Any]] = []

    with rasterio.open(reference_path) as src:
        transform = src.transform
        crs = src.crs

    for idx, (geom, value) in enumerate(
        shapes(mask.astype(np.uint8), mask=mask.astype(np.uint8), transform=transform),
        start=1,
    ):
        if int(value) != 1:
            continue

        geom_native = shape(geom)
        geom_wgs84 = reproject_geometry(geom_native, str(crs), "EPSG:4326")
        area_m2 = abs(GEOD.geometry_area_perimeter(geom_wgs84)[0])
        centroid = geom_wgs84.centroid

        record = {
            id_field: idx,
            "area_m2": float(area_m2),
            "centroid_lon": float(centroid.x),
            "centroid_lat": float(centroid.y),
            "release_t1": context.release_t1,
            "release_t2": context.release_t2,
            "src_date_t1": context.src_date_t1,
            "src_date_t2": context.src_date_t2,
        }
        records.append(record)
        features_out.append(
            {
                "type": "Feature",
                "geometry": mapping(geom_wgs84),
                "properties": record,
            }
        )

    return pd.DataFrame(records), {"type": "FeatureCollection", "features": features_out}


def vectorize_new_buildings(
    mask: np.ndarray,
    reference_path: Path,
    context: VectorizationContext,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    return _vectorize_regions(mask, reference_path, context, id_field="building_id")


def vectorize_change_regions(
    mask: np.ndarray,
    reference_path: Path,
    context: VectorizationContext,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    return _vectorize_regions(mask, reference_path, context, id_field="change_id")


def vectorize_segmentation_regions(
    mask: np.ndarray,
    reference_path: Path,
    context: SegmentationVectorizationContext,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    features_out: list[dict[str, Any]] = []

    with rasterio.open(reference_path) as src:
        transform = src.transform
        crs = src.crs

    for idx, (geom, value) in enumerate(
        shapes(mask.astype(np.uint8), mask=mask.astype(np.uint8), transform=transform),
        start=1,
    ):
        if int(value) != 1:
            continue

        geom_native = shape(geom)
        geom_wgs84 = reproject_geometry(geom_native, str(crs), "EPSG:4326")
        area_m2 = abs(GEOD.geometry_area_perimeter(geom_wgs84)[0])
        centroid = geom_wgs84.centroid

        record = {
            "segment_id": idx,
            "area_m2": float(area_m2),
            "centroid_lon": float(centroid.x),
            "centroid_lat": float(centroid.y),
            "release": context.release,
            "src_date": context.src_date,
            "prompt": context.prompt,
        }
        records.append(record)
        features_out.append(
            {
                "type": "Feature",
                "geometry": mapping(geom_wgs84),
                "properties": record,
            }
        )

    return pd.DataFrame(records), {"type": "FeatureCollection", "features": features_out}


def _iter_metric_geometries(feature_collection: dict[str, Any]) -> tuple[str, list[BaseGeometry], list[dict[str, Any]]]:
    features = feature_collection.get("features", [])
    if not features:
        return "EPSG:4326", [], []
    source_geoms = [shape(feature["geometry"]) for feature in features]
    lon, lat = centroid_lonlat(unary_union(source_geoms))
    metric_crs = f"EPSG:{utm_epsg_from_lonlat(lon, lat)}"
    metric_geoms = [reproject_geometry(geom, "EPSG:4326", metric_crs) for geom in source_geoms]
    return metric_crs, metric_geoms, [feature.get("properties", {}) for feature in features]


def _metric_crs_from_aoi(aoi_geojson: dict[str, Any]) -> tuple[str, BaseGeometry]:
    aoi_geometry = parse_aoi_geometry(aoi_geojson)
    lon, lat = centroid_lonlat(aoi_geometry)
    metric_crs = f"EPSG:{utm_epsg_from_lonlat(lon, lat)}"
    return metric_crs, aoi_geometry


def _normalize_polygon_feature_collection_to_metric_geometries(
    feature_collection: dict[str, Any],
    *,
    metric_crs: str,
    clip_metric_geometry: BaseGeometry | None = None,
) -> tuple[list[BaseGeometry], list[dict[str, Any]]]:
    features = feature_collection.get("features", [])
    metric_geometries: list[BaseGeometry] = []
    properties: list[dict[str, Any]] = []

    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            continue
        try:
            geometry = shape(geometry_payload).buffer(0)
        except Exception:
            continue
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue

        metric_geometry = reproject_geometry(geometry, "EPSG:4326", metric_crs).buffer(0)
        if clip_metric_geometry is not None:
            metric_geometry = metric_geometry.intersection(clip_metric_geometry).buffer(0)
        if metric_geometry.is_empty:
            continue
        if metric_geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            metric_geometry = metric_geometry.buffer(0)
        if metric_geometry.is_empty or metric_geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue

        metric_geometries.append(metric_geometry)
        properties.append(dict(feature.get("properties") or {}))

    return metric_geometries, properties


def _pairwise_distance_connector(source_geometry: BaseGeometry, target_geometry: BaseGeometry) -> LineString:
    source_point, target_point = nearest_points(source_geometry, target_geometry)
    return LineString([source_point, target_point])


def _edge_crosses_road_barrier(
    source_geometry: BaseGeometry,
    target_geometry: BaseGeometry,
    road_geometries_metric: list[BaseGeometry],
) -> bool:
    if not road_geometries_metric:
        return False
    connector = _pairwise_distance_connector(source_geometry, target_geometry)
    if connector.length == 0:
        return False
    return any(connector.intersects(road_geometry) for road_geometry in road_geometries_metric)


def _connected_components_by_distance(
    metric_geometries: list[BaseGeometry],
    *,
    max_gap_m: float,
    road_geometries_metric: list[BaseGeometry] | None = None,
) -> list[list[int]]:
    if not metric_geometries:
        return []

    road_geometries_metric = road_geometries_metric or []
    parent = list(range(len(metric_geometries)))
    rank = [0] * len(metric_geometries)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left_index: int, right_index: int) -> None:
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            parent[left_root] = right_root
        elif rank[left_root] > rank[right_root]:
            parent[right_root] = left_root
        else:
            parent[right_root] = left_root
            rank[left_root] += 1

    for left_index, left_geometry in enumerate(metric_geometries):
        for right_index in range(left_index + 1, len(metric_geometries)):
            right_geometry = metric_geometries[right_index]
            if left_geometry.distance(right_geometry) > float(max_gap_m):
                continue
            if _edge_crosses_road_barrier(left_geometry, right_geometry, road_geometries_metric):
                continue
            union(left_index, right_index)

    grouped_components: dict[int, list[int]] = {}
    for index in range(len(metric_geometries)):
        root = find(index)
        grouped_components.setdefault(root, []).append(index)
    return [grouped_components[root] for root in sorted(grouped_components, key=lambda item: min(grouped_components[item]))]


def _polygonal_union_geometry(geometries: list[BaseGeometry]) -> BaseGeometry:
    if not geometries:
        return GeometryCollection()
    dissolved = unary_union([geometry for geometry in geometries if geometry is not None and not geometry.is_empty]).buffer(0)
    if dissolved.is_empty:
        return GeometryCollection()
    if dissolved.geom_type in {"Polygon", "MultiPolygon"}:
        return dissolved
    polygonal_parts = [part for part in getattr(dissolved, "geoms", []) if part.geom_type in {"Polygon", "MultiPolygon"} and not part.is_empty]
    if not polygonal_parts:
        return GeometryCollection()
    return unary_union(polygonal_parts).buffer(0)


def _closed_block_geometry(geometries: list[BaseGeometry], *, max_gap_m: float) -> BaseGeometry:
    polygonal_geometry = _polygonal_union_geometry(geometries)
    if polygonal_geometry.is_empty:
        return GeometryCollection()

    gap = max(0.0, float(max_gap_m))
    if gap <= 0:
        return polygonal_geometry

    half_gap = gap / 2.0
    grown = [geometry.buffer(half_gap) for geometry in geometries if geometry is not None and not geometry.is_empty]
    closed = unary_union(grown).buffer(-half_gap).buffer(0)
    if closed.is_empty:
        return polygonal_geometry
    if closed.geom_type in {"Polygon", "MultiPolygon"}:
        return closed
    polygonal_parts = [part for part in getattr(closed, "geoms", []) if part.geom_type in {"Polygon", "MultiPolygon"} and not part.is_empty]
    if not polygonal_parts:
        return polygonal_geometry
    return unary_union(polygonal_parts).buffer(0)


def _fill_small_holes(geometry: BaseGeometry, *, max_area_m2: float) -> BaseGeometry:
    if geometry.is_empty:
        return geometry
    if isinstance(geometry, Polygon):
        retained_interiors = []
        for ring in geometry.interiors:
            hole = Polygon(ring)
            if hole.area >= float(max_area_m2):
                retained_interiors.append(ring)
        return Polygon(geometry.exterior, retained_interiors).buffer(0)
    if isinstance(geometry, MultiPolygon):
        return unary_union([_fill_small_holes(part, max_area_m2=max_area_m2) for part in geometry.geoms]).buffer(0)
    if isinstance(geometry, GeometryCollection):
        polygon_parts = [
            _fill_small_holes(part, max_area_m2=max_area_m2)
            for part in geometry.geoms
            if part.geom_type in {"Polygon", "MultiPolygon"} and not part.is_empty
        ]
        if not polygon_parts:
            return GeometryCollection()
        return unary_union(polygon_parts).buffer(0)
    return geometry.buffer(0)


def _remove_all_holes(geometry: BaseGeometry) -> BaseGeometry:
    if geometry.is_empty:
        return geometry
    if isinstance(geometry, Polygon):
        return Polygon(geometry.exterior).buffer(0)
    if isinstance(geometry, MultiPolygon):
        return unary_union([_remove_all_holes(part) for part in geometry.geoms]).buffer(0)
    if isinstance(geometry, GeometryCollection):
        polygon_parts = [
            _remove_all_holes(part)
            for part in geometry.geoms
            if part.geom_type in {"Polygon", "MultiPolygon"} and not part.is_empty
        ]
        if not polygon_parts:
            return GeometryCollection()
        return unary_union(polygon_parts).buffer(0)
    return geometry.buffer(0)


def _has_interior_holes(geometry: BaseGeometry) -> bool:
    if geometry.is_empty:
        return False
    if isinstance(geometry, Polygon):
        return len(geometry.interiors) > 0
    if isinstance(geometry, MultiPolygon):
        return any(len(part.interiors) > 0 for part in geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return any(_has_interior_holes(part) for part in geometry.geoms)
    return False


def _coverage_missing_area_m2(source_geometry: BaseGeometry, envelope_geometry: BaseGeometry) -> float:
    if source_geometry.is_empty:
        return 0.0
    if envelope_geometry.is_empty:
        return float(source_geometry.area)
    try:
        missing = source_geometry.difference(envelope_geometry.buffer(0.01)).buffer(0)
    except Exception:
        missing = source_geometry.difference(envelope_geometry).buffer(0)
    return 0.0 if missing.is_empty else float(missing.area)


def _simplify_polygonal_geometry(geometry: BaseGeometry, *, tolerance_m: float) -> BaseGeometry:
    if geometry.is_empty or tolerance_m <= 0:
        return geometry
    simplified = geometry.simplify(float(tolerance_m), preserve_topology=True)
    if simplified.is_empty:
        return geometry
    if simplified.geom_type not in {"Polygon", "MultiPolygon"}:
        simplified = simplified.buffer(0)
    if simplified.is_empty:
        return geometry
    return simplified


def _tighten_growth_envelope_geometry(
    geometry: BaseGeometry,
    *,
    bridge_m: float,
    ratio: float,
    simplify_tolerance_m: float,
    fill_holes_max_area_m2: float,
) -> BaseGeometry:
    if geometry.is_empty:
        return geometry

    source_geometry = geometry.buffer(float(bridge_m))
    if source_geometry.is_empty:
        source_geometry = geometry

    tightened = source_geometry
    if concave_hull is not None:
        try:
            tightened = concave_hull(source_geometry, ratio=float(ratio), allow_holes=False)
        except Exception:
            tightened = source_geometry

    if tightened.is_empty:
        tightened = source_geometry

    if bridge_m > 0:
        constraint = geometry.buffer(float(bridge_m) * 1.5)
        if not constraint.is_empty:
            tightened = tightened.intersection(constraint).buffer(0)

    if tightened.is_empty:
        tightened = geometry

    tightened = _fill_small_holes(tightened, max_area_m2=fill_holes_max_area_m2)
    tightened = _simplify_polygonal_geometry(tightened, tolerance_m=simplify_tolerance_m)
    if tightened.is_empty:
        return geometry
    if tightened.geom_type not in {"Polygon", "MultiPolygon"}:
        tightened = tightened.buffer(0)
    if tightened.is_empty:
        return geometry
    return tightened


def _growth_envelope_ratio_candidates(initial_ratio: float) -> list[float]:
    candidates = [
        float(initial_ratio),
        0.18,
        0.28,
        0.42,
        0.60,
        0.80,
        1.0,
    ]
    normalized: list[float] = []
    for candidate in candidates:
        candidate = max(0.0, min(1.0, candidate))
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _finalize_growth_envelope_candidate(
    candidate_geometry: BaseGeometry,
    *,
    source_geometry: BaseGeometry,
    aoi_geometry: BaseGeometry,
    simplify_tolerance_m: float,
    coverage_tolerance_m2: float,
) -> BaseGeometry:
    if candidate_geometry.is_empty:
        return GeometryCollection()

    envelope = candidate_geometry.buffer(0)
    if envelope.is_empty:
        return GeometryCollection()
    if envelope.geom_type not in {"Polygon", "MultiPolygon"}:
        envelope = envelope.buffer(0)
    if envelope.is_empty or envelope.geom_type not in {"Polygon", "MultiPolygon"}:
        return GeometryCollection()

    envelope = _remove_all_holes(envelope)
    unsimplified_envelope = envelope
    simplified_envelope = _remove_all_holes(_simplify_polygonal_geometry(envelope, tolerance_m=simplify_tolerance_m))
    if (
        not simplified_envelope.is_empty
        and simplified_envelope.geom_type in {"Polygon", "MultiPolygon"}
        and _coverage_missing_area_m2(source_geometry, simplified_envelope) <= coverage_tolerance_m2
    ):
        envelope = simplified_envelope
    else:
        envelope = unsimplified_envelope
    envelope = envelope.intersection(aoi_geometry).buffer(0)
    if envelope.is_empty:
        return GeometryCollection()
    if envelope.geom_type not in {"Polygon", "MultiPolygon"}:
        envelope = envelope.buffer(0)
    if envelope.is_empty or envelope.geom_type not in {"Polygon", "MultiPolygon"}:
        return GeometryCollection()

    # Clipping can reintroduce interior rings when an AOI itself has holes. For
    # regular AOIs, enforce a true no-hole envelope.
    without_holes = _remove_all_holes(envelope)
    if without_holes.difference(aoi_geometry).area <= coverage_tolerance_m2:
        envelope = without_holes.intersection(aoi_geometry).buffer(0)

    if envelope.is_empty or envelope.geom_type not in {"Polygon", "MultiPolygon"}:
        return GeometryCollection()
    if _has_interior_holes(envelope):
        return GeometryCollection()
    if _coverage_missing_area_m2(source_geometry, envelope) > coverage_tolerance_m2:
        return GeometryCollection()
    return envelope


def _build_single_concave_growth_envelope(
    source_geometry: BaseGeometry,
    *,
    aoi_geometry: BaseGeometry,
    bridge_m: float,
    hull_ratio: float,
    simplify_tolerance_m: float,
) -> tuple[BaseGeometry, float, str]:
    if source_geometry.is_empty:
        return GeometryCollection(), float(hull_ratio), "empty"

    coverage_tolerance_m2 = max(0.05, source_geometry.area * 1e-9)
    hull_source = source_geometry.buffer(float(bridge_m)).buffer(0) if bridge_m > 0 else source_geometry
    if hull_source.is_empty:
        hull_source = source_geometry

    if concave_hull is not None:
        for ratio in _growth_envelope_ratio_candidates(hull_ratio):
            try:
                candidate = concave_hull(hull_source, ratio=ratio, allow_holes=False)
            except Exception:
                continue
            envelope = _finalize_growth_envelope_candidate(
                candidate,
                source_geometry=source_geometry,
                aoi_geometry=aoi_geometry,
                simplify_tolerance_m=simplify_tolerance_m,
                coverage_tolerance_m2=coverage_tolerance_m2,
            )
            if not envelope.is_empty:
                return envelope, ratio, "concave_hull"

    fallback = _finalize_growth_envelope_candidate(
        source_geometry.convex_hull,
        source_geometry=source_geometry,
        aoi_geometry=aoi_geometry,
        simplify_tolerance_m=simplify_tolerance_m,
        coverage_tolerance_m2=coverage_tolerance_m2,
    )
    return fallback, 1.0, "convex_hull_fallback"


def _feature_collection_from_metric_geometries(
    geometries: list[BaseGeometry],
    *,
    metric_crs: str,
    properties_list: list[dict[str, Any]],
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    for geometry, properties in zip(geometries, properties_list):
        if geometry.is_empty:
            continue
        geometry_wgs84 = reproject_geometry(geometry, metric_crs, "EPSG:4326").buffer(0)
        if geometry_wgs84.is_empty:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(geometry_wgs84),
                "properties": dict(properties),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _build_temporal_geometry_layer(
    source_geojson: dict[str, Any],
    *,
    aoi_geojson: dict[str, Any],
    release_identifier: str,
    release_date: str | None,
    kind: str,
    temporal_block_gap_m: float,
    road_constraint_layer_path: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    metric_crs, aoi_geometry = _metric_crs_from_aoi(aoi_geojson)
    aoi_metric = reproject_geometry(aoi_geometry, "EPSG:4326", metric_crs).buffer(0)
    metric_geometries, _ = _normalize_polygon_feature_collection_to_metric_geometries(
        source_geojson,
        metric_crs=metric_crs,
        clip_metric_geometry=aoi_metric,
    )
    if not metric_geometries:
        return pd.DataFrame(), empty_feature_collection()

    road_geometries_metric = _load_metric_road_geometries(road_constraint_layer_path, metric_crs=metric_crs)
    components = _connected_components_by_distance(
        metric_geometries,
        max_gap_m=temporal_block_gap_m,
        road_geometries_metric=road_geometries_metric,
    )

    records: list[dict[str, Any]] = []
    geometries: list[BaseGeometry] = []
    properties_list: list[dict[str, Any]] = []

    for block_index, component in enumerate(components, start=1):
        component_geometries = [metric_geometries[item] for item in component]
        component_geometry = _closed_block_geometry(component_geometries, max_gap_m=temporal_block_gap_m)
        if component_geometry.is_empty:
            continue
        component_geometry = component_geometry.intersection(aoi_metric).buffer(0)
        if component_geometry.is_empty:
            continue
        if component_geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            component_geometry = component_geometry.buffer(0)
        if component_geometry.is_empty:
            continue

        geometry_wgs84 = reproject_geometry(component_geometry, metric_crs, "EPSG:4326").buffer(0)
        if geometry_wgs84.is_empty:
            continue

        centroid = geometry_wgs84.centroid
        record = {
            "block_id": block_index,
            "release_identifier": release_identifier,
            "release_date": release_date,
            "source_building_count": int(len(component)),
            "cluster_gap_m": float(temporal_block_gap_m),
            "kind": kind,
            "area_m2": float(geodesic_area_m2(geometry_wgs84)),
            "centroid_lon": float(centroid.x),
            "centroid_lat": float(centroid.y),
        }
        records.append(record)
        geometries.append(component_geometry)
        properties_list.append(record)

    if not geometries:
        return pd.DataFrame(), empty_feature_collection()

    return pd.DataFrame(records), _feature_collection_from_metric_geometries(
        geometries,
        metric_crs=metric_crs,
        properties_list=properties_list,
    )


def build_temporal_growth_blocks(
    source_geojson: dict[str, Any],
    *,
    aoi_geojson: dict[str, Any],
    release_identifier: str,
    release_date: str | None,
    kind: str,
    temporal_block_gap_m: float = 20.0,
    road_constraint_layer_path: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    return _build_temporal_geometry_layer(
        source_geojson,
        aoi_geojson=aoi_geojson,
        release_identifier=release_identifier,
        release_date=release_date,
        kind=kind,
        temporal_block_gap_m=temporal_block_gap_m,
        road_constraint_layer_path=road_constraint_layer_path,
    )


def build_temporal_growth_envelope(
    cumulative_union_geojson: dict[str, Any],
    *,
    aoi_geojson: dict[str, Any],
    release_identifier: str,
    release_date: str | None,
    temporal_block_gap_m: float = 20.0,
    envelope_component_gap_m: float | None = None,
    envelope_bridge_m: float | None = None,
    envelope_hull_ratio: float | None = None,
    envelope_simplify_tolerance_m: float | None = None,
    envelope_fill_holes_max_area_m2: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    metric_crs, aoi_geometry = _metric_crs_from_aoi(aoi_geojson)
    aoi_metric = reproject_geometry(aoi_geometry, "EPSG:4326", metric_crs).buffer(0)
    source_geometries, source_properties = _normalize_polygon_feature_collection_to_metric_geometries(
        cumulative_union_geojson,
        metric_crs=metric_crs,
        clip_metric_geometry=aoi_metric,
    )
    if not source_geometries:
        return pd.DataFrame(), empty_feature_collection()

    cumulative_union = _polygonal_union_geometry(source_geometries).intersection(aoi_metric).buffer(0)
    if cumulative_union.is_empty:
        return pd.DataFrame(), empty_feature_collection()

    component_gap_m = (
        float(envelope_component_gap_m)
        if envelope_component_gap_m is not None
        else 0.0
    )
    bridge_m = (
        float(envelope_bridge_m)
        if envelope_bridge_m is not None
        else 0.0
    )
    hull_ratio = float(envelope_hull_ratio) if envelope_hull_ratio is not None else 0.12
    simplify_tolerance_m = (
        float(envelope_simplify_tolerance_m)
        if envelope_simplify_tolerance_m is not None
        else 0.75
    )
    fill_holes_max_area_m2 = float(envelope_fill_holes_max_area_m2) if envelope_fill_holes_max_area_m2 is not None else None

    envelope, effective_hull_ratio, envelope_method = _build_single_concave_growth_envelope(
        cumulative_union,
        aoi_geometry=aoi_metric,
        bridge_m=bridge_m,
        hull_ratio=hull_ratio,
        simplify_tolerance_m=simplify_tolerance_m,
    )
    if envelope.is_empty:
        return pd.DataFrame(), empty_feature_collection()

    source_building_count = int(
        sum(int(props.get("source_building_count", 0) or 0) for props in source_properties)
    )
    if source_building_count == 0:
        source_building_count = int(len(source_geometries))
    source_block_count = int(len(source_geometries))
    geometry_wgs84 = reproject_geometry(envelope, metric_crs, "EPSG:4326").buffer(0)
    if geometry_wgs84.is_empty:
        return pd.DataFrame(), empty_feature_collection()

    centroid = geometry_wgs84.centroid
    record = {
        "release_identifier": release_identifier,
        "release_date": release_date,
        "source_block_count": source_block_count,
        "source_building_count": source_building_count,
        "cluster_gap_m": float(temporal_block_gap_m),
        "envelope_component_gap_m": float(component_gap_m),
        "envelope_bridge_m": float(bridge_m),
        "envelope_hull_ratio": float(effective_hull_ratio),
        "requested_envelope_hull_ratio": float(hull_ratio),
        "envelope_method": envelope_method,
        "envelope_simplify_tolerance_m": float(simplify_tolerance_m),
        "envelope_fill_holes_max_area_m2": fill_holes_max_area_m2,
        "envelope_hole_policy": "remove_all",
        "envelope_has_holes": bool(_has_interior_holes(envelope)),
        "envelope_missing_source_area_m2": float(_coverage_missing_area_m2(cumulative_union, envelope)),
        "kind": "cumulative_growth_envelope",
        "area_m2": float(geodesic_area_m2(geometry_wgs84)),
        "centroid_lon": float(centroid.x),
        "centroid_lat": float(centroid.y),
    }

    return (
        pd.DataFrame([record]),
        _feature_collection_from_metric_geometries(
            [envelope],
            metric_crs=metric_crs,
            properties_list=[record],
        ),
    )


def _load_metric_road_geometries(
    road_constraint_layer_path: str | None,
    *,
    metric_crs: str,
) -> list[BaseGeometry]:
    if not road_constraint_layer_path:
        return []
    road_path = Path(road_constraint_layer_path)
    if not road_path.exists():
        return []
    roads = gpd.read_file(road_path)
    if roads.empty or roads.crs is None:
        return []
    roads = roads[roads.geometry.notna()].copy()
    if roads.empty:
        return []
    roads = roads.to_crs(metric_crs)
    geometries = [
        geom
        for geom in roads.geometry
        if geom is not None and not geom.is_empty and geom.geom_type in {"LineString", "MultiLineString"}
    ]
    return geometries


def _constrain_buffer_outer_shell(
    buffered_metric: BaseGeometry,
    source_metric: BaseGeometry,
    road_geometries_metric: list[BaseGeometry],
) -> BaseGeometry:
    if buffered_metric.is_empty or not road_geometries_metric:
        return buffered_metric
    candidate_roads = [road for road in road_geometries_metric if road.intersects(buffered_metric)]
    if not candidate_roads:
        return buffered_metric
    splitter = unary_union(candidate_roads)
    if splitter.is_empty:
        return buffered_metric
    try:
        split_parts = split(buffered_metric, splitter)
    except Exception:
        return buffered_metric
    if isinstance(split_parts, GeometryCollection):
        parts = [geom for geom in split_parts.geoms if not geom.is_empty and geom.area > 0]
    else:
        parts = [geom for geom in split_parts.geoms if not geom.is_empty and geom.area > 0]
    kept_parts = [geom for geom in parts if geom.intersects(source_metric)]
    if not kept_parts:
        return buffered_metric
    constrained = unary_union(kept_parts).buffer(0)
    if constrained.is_empty:
        return buffered_metric
    return constrained


def _polygon_parts_without_holes(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        cleaned = Polygon(geometry.exterior)
        return [cleaned] if not cleaned.is_empty and cleaned.area > 0 else []
    if isinstance(geometry, MultiPolygon):
        parts: list[Polygon] = []
        for part in geometry.geoms:
            parts.extend(_polygon_parts_without_holes(part))
        return parts
    if isinstance(geometry, GeometryCollection):
        parts: list[Polygon] = []
        for part in geometry.geoms:
            parts.extend(_polygon_parts_without_holes(part))
        return parts
    return []


def _clean_polygonal_geometry(
    geometry: BaseGeometry,
    *,
    keep_disjoint_parts_separate: bool,
) -> list[Polygon]:
    parts = _polygon_parts_without_holes(geometry.buffer(0) if not geometry.is_empty else geometry)
    if not parts:
        return []
    if keep_disjoint_parts_separate:
        return parts
    merged = unary_union(parts).buffer(0)
    return _polygon_parts_without_holes(merged)


def _merge_close_regions(
    regions_geojson: dict[str, Any],
    *,
    max_gap_m: float,
    context: VectorizationContext,
    id_field: str = "building_id",
    source_count_field: str,
    gap_field: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    metric_crs, metric_geoms, source_properties = _iter_metric_geometries(regions_geojson)
    if not metric_geoms:
        return pd.DataFrame(), empty_feature_collection()

    half_gap = float(max_gap_m) / 2.0
    grown = [geom.buffer(half_gap) for geom in metric_geoms]
    merged_geom = unary_union(grown).buffer(-half_gap)
    if merged_geom.is_empty:
        return pd.DataFrame(), empty_feature_collection()

    if merged_geom.geom_type == "Polygon":
        merged_parts = [merged_geom]
    else:
        merged_parts = [geom.buffer(0) for geom in merged_geom.geoms if not geom.is_empty and geom.area > 0]

    records: list[dict[str, Any]] = []
    features_out: list[dict[str, Any]] = []
    for idx, geom_metric in enumerate(merged_parts, start=1):
        geom_wgs84 = reproject_geometry(geom_metric, metric_crs, "EPSG:4326")
        centroid = geom_wgs84.centroid
        source_polygon_count = sum(1 for item in metric_geoms if item.intersects(geom_metric))
        record = {
            id_field: idx,
            "area_m2": float(geom_metric.area),
            "centroid_lon": float(centroid.x),
            "centroid_lat": float(centroid.y),
            source_count_field: int(source_polygon_count),
            gap_field: float(max_gap_m),
            "release_t1": context.release_t1,
            "release_t2": context.release_t2,
            "src_date_t1": context.src_date_t1,
            "src_date_t2": context.src_date_t2,
        }
        if source_properties:
            sample = source_properties[0]
            for key in ("release_t1", "release_t2", "src_date_t1", "src_date_t2"):
                if sample.get(key) is not None:
                    record[key] = sample.get(key)
        records.append(record)
        features_out.append(
            {
                "type": "Feature",
                "geometry": mapping(geom_wgs84),
                "properties": record,
            }
        )
    return pd.DataFrame(records), {"type": "FeatureCollection", "features": features_out}


def merge_close_buildings(
    buildings_geojson: dict[str, Any],
    *,
    max_gap_m: float,
    context: VectorizationContext,
    id_field: str = "building_id",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    return _merge_close_regions(
        buildings_geojson,
        max_gap_m=max_gap_m,
        context=context,
        id_field=id_field,
        source_count_field="source_polygon_count",
        gap_field="merge_gap_m",
    )


def merge_close_change_regions(
    regions_geojson: dict[str, Any],
    *,
    max_gap_m: float,
    context: VectorizationContext,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    return _merge_close_regions(
        regions_geojson,
        max_gap_m=max_gap_m,
        context=context,
        id_field="change_id",
        source_count_field="source_change_count",
        gap_field="merge_gap_m",
    )


def build_building_blocks(
    buildings_geojson: dict[str, Any],
    *,
    max_gap_m: float,
    context: VectorizationContext,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    blocks_df, blocks_geojson = _merge_close_regions(
        buildings_geojson,
        max_gap_m=max_gap_m,
        context=context,
        id_field="block_id",
        source_count_field="source_polygon_count",
        gap_field="merge_gap_m",
    )
    if blocks_df.empty:
        return blocks_df, blocks_geojson
    blocks_df = blocks_df.rename(
        columns={
            "source_polygon_count": "source_building_count",
            "merge_gap_m": "block_gap_m",
        }
    )
    for feature in blocks_geojson["features"]:
        props = feature["properties"]
        props["source_building_count"] = props.pop("source_polygon_count")
        props["block_gap_m"] = props.pop("merge_gap_m")
    return blocks_df, blocks_geojson


def build_change_blocks(
    regions_geojson: dict[str, Any],
    *,
    max_gap_m: float,
    context: VectorizationContext,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    return _merge_close_regions(
        regions_geojson,
        max_gap_m=max_gap_m,
        context=context,
        id_field="change_block_id",
        source_count_field="source_change_count",
        gap_field="block_gap_m",
    )


def _build_metric_buffer_layers(
    source_geojson: dict[str, Any],
    *,
    distances_m: list[float],
    context: VectorizationContext,
    source_id_property: str,
    source_id_output_field: str,
    source_count_field: str,
    keep_disjoint_parts_separate: bool = True,
    road_constraint_layer_path: str | None = None,
) -> dict[str, tuple[pd.DataFrame, dict[str, Any]]]:
    metric_crs, metric_geoms, source_properties = _iter_metric_geometries(source_geojson)
    if not metric_geoms:
        return {}
    road_geometries_metric = _load_metric_road_geometries(road_constraint_layer_path, metric_crs=metric_crs)

    outputs: dict[str, tuple[pd.DataFrame, dict[str, Any]]] = {}
    for distance in distances_m:
        records: list[dict[str, Any]] = []
        features_out: list[dict[str, Any]] = []
        next_buffer_id = 1
        for source_index, (geom_metric, source_props) in enumerate(zip(metric_geoms, source_properties), start=1):
            buffered_metric = geom_metric.buffer(float(distance))
            buffered_metric = _constrain_buffer_outer_shell(
                buffered_metric,
                geom_metric,
                road_geometries_metric,
            )
            cleaned_parts = _clean_polygonal_geometry(
                buffered_metric,
                keep_disjoint_parts_separate=keep_disjoint_parts_separate,
            )
            if not cleaned_parts:
                continue
            for part_index, part_metric in enumerate(cleaned_parts, start=1):
                buffered_wgs84 = reproject_geometry(part_metric, metric_crs, "EPSG:4326")
                centroid = buffered_wgs84.centroid
                record = {
                    "buffer_id": next_buffer_id,
                    "buffer_part_index": part_index,
                    source_id_output_field: source_props.get(source_id_property, source_index),
                    "buffer_m": float(distance),
                    "area_m2": float(part_metric.area),
                    "centroid_lon": float(centroid.x),
                    "centroid_lat": float(centroid.y),
                    source_count_field: source_props.get(source_count_field),
                    "block_gap_m": source_props.get("block_gap_m"),
                    "release_t1": context.release_t1,
                    "release_t2": context.release_t2,
                    "src_date_t1": context.src_date_t1,
                    "src_date_t2": context.src_date_t2,
                }
                records.append(record)
                features_out.append(
                    {
                        "type": "Feature",
                        "geometry": mapping(buffered_wgs84),
                        "properties": record,
                    }
                )
                next_buffer_id += 1
        outputs[f"{int(distance)}m"] = (
            pd.DataFrame(records),
            {"type": "FeatureCollection", "features": features_out},
        )
    return outputs


def build_metric_buffer_layers(
    source_geojson: dict[str, Any],
    *,
    distances_m: list[float],
    context: VectorizationContext,
    keep_disjoint_parts_separate: bool = True,
    road_constraint_layer_path: str | None = None,
) -> dict[str, tuple[pd.DataFrame, dict[str, Any]]]:
    return _build_metric_buffer_layers(
        source_geojson,
        distances_m=distances_m,
        context=context,
        source_id_property="block_id",
        source_id_output_field="source_block_id",
        source_count_field="source_building_count",
        keep_disjoint_parts_separate=keep_disjoint_parts_separate,
        road_constraint_layer_path=road_constraint_layer_path,
    )


def build_change_buffer_layers(
    source_geojson: dict[str, Any],
    *,
    distances_m: list[float],
    context: VectorizationContext,
    keep_disjoint_parts_separate: bool = True,
    road_constraint_layer_path: str | None = None,
) -> dict[str, tuple[pd.DataFrame, dict[str, Any]]]:
    return _build_metric_buffer_layers(
        source_geojson,
        distances_m=distances_m,
        context=context,
        source_id_property="change_block_id",
        source_id_output_field="source_change_block_id",
        source_count_field="source_change_count",
        keep_disjoint_parts_separate=keep_disjoint_parts_separate,
        road_constraint_layer_path=road_constraint_layer_path,
    )
