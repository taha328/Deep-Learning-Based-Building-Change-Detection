from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import ndimage
from shapely.geometry import GeometryCollection, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.utils.geometry import centroid_lonlat, reproject_geometry, utm_epsg_from_lonlat


def dilate_mask(mask: np.ndarray, pixels: int) -> np.ndarray:
    if pixels <= 0:
        return mask.astype(bool)
    structure = np.ones((3, 3), dtype=bool)
    return ndimage.binary_dilation(mask.astype(bool), structure=structure, iterations=int(pixels))


def remove_small_components(mask: np.ndarray, min_pixels: int) -> tuple[np.ndarray, np.ndarray]:
    labeled, num = ndimage.label(mask.astype(np.uint8))
    keep = np.zeros_like(mask, dtype=bool)
    for label_id in range(1, num + 1):
        component = labeled == label_id
        if int(component.sum()) >= min_pixels:
            keep |= component
    kept_labels, _ = ndimage.label(keep.astype(np.uint8))
    return keep, kept_labels


def suppress_edge_hugging_components(
    mask: np.ndarray,
    *,
    reference_mask: np.ndarray,
    min_core_distance_pixels: int,
    min_core_pixels: int = 1,
) -> np.ndarray:
    candidate_mask = mask.astype(bool)
    if min_core_distance_pixels <= 0 or not candidate_mask.any():
        return candidate_mask

    reference = reference_mask.astype(bool)
    distance_outside_reference = ndimage.distance_transform_edt(~reference)
    core_mask = candidate_mask & (distance_outside_reference >= float(min_core_distance_pixels))

    labeled, num = ndimage.label(candidate_mask.astype(np.uint8))
    keep = np.zeros_like(candidate_mask, dtype=bool)
    for label_id in range(1, num + 1):
        component = labeled == label_id
        if int((core_mask & component).sum()) >= max(1, int(min_core_pixels)):
            keep |= component
    return keep


@dataclass(frozen=True)
class AdditionCandidateFilterSettings:
    min_area_m2: float = 8.0
    max_existing_overlap_ratio: float = 0.50
    thin_artifact_max_area_m2: float = 80.0
    thinness_min_ratio: float = 0.20
    edge_buffer_m: float = 2.0
    max_edge_overlap_ratio: float = 0.60
    thin_artifact_max_mean_probability: float = 0.75


@dataclass(frozen=True)
class AdditionCandidateFilterResult:
    kept_geojson: dict[str, Any]
    rejected_geojson: dict[str, Any]
    flagged_geojson: dict[str, Any]
    diagnostics_geojson: dict[str, Any]


def _feature_collection_geometries(feature_collection: dict[str, Any] | None) -> list[BaseGeometry]:
    if not feature_collection:
        return []
    geometries: list[BaseGeometry] = []
    for feature in feature_collection.get("features", []):
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            continue
        try:
            geometry = shape(feature["geometry"]).buffer(0)
        except Exception:
            continue
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        geometries.append(geometry)
    return geometries


def _metric_crs_for_collections(*feature_collections: dict[str, Any] | None) -> str:
    geometries: list[BaseGeometry] = []
    for feature_collection in feature_collections:
        geometries.extend(_feature_collection_geometries(feature_collection))
    if not geometries:
        return "EPSG:3857"
    lon, lat = centroid_lonlat(unary_union(geometries))
    return f"EPSG:{utm_epsg_from_lonlat(lon, lat)}"


def _union_metric_geometry(feature_collection: dict[str, Any] | None, *, metric_crs: str) -> BaseGeometry:
    geometries = [
        reproject_geometry(geometry, "EPSG:4326", metric_crs).buffer(0)
        for geometry in _feature_collection_geometries(feature_collection)
    ]
    if not geometries:
        return GeometryCollection()
    return unary_union(geometries).buffer(0)


def _thinness_ratio(geometry: BaseGeometry) -> float | None:
    if geometry.is_empty:
        return None
    rectangle = geometry.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords) if hasattr(rectangle, "exterior") else []
    if len(coords) < 4:
        return None
    side_lengths = [
        float(np.hypot(coords[index + 1][0] - coords[index][0], coords[index + 1][1] - coords[index][1]))
        for index in range(4)
    ]
    positive_lengths = [length for length in side_lengths if length > 0]
    if not positive_lengths:
        return None
    long_side = max(positive_lengths)
    short_side = min(positive_lengths)
    if long_side <= 0:
        return None
    return float(short_side / long_side)


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(max(0.0, min(1.0, numerator / denominator)))


def filter_addition_candidates(
    candidates_geojson: dict[str, Any],
    *,
    existing_footprint_geojson: dict[str, Any] | None,
    settings: AdditionCandidateFilterSettings,
) -> AdditionCandidateFilterResult:
    metric_crs = _metric_crs_for_collections(candidates_geojson, existing_footprint_geojson)
    existing_metric = _union_metric_geometry(existing_footprint_geojson, metric_crs=metric_crs)
    old_edge_band = GeometryCollection()
    if not existing_metric.is_empty and settings.edge_buffer_m > 0:
        outer = existing_metric.buffer(float(settings.edge_buffer_m))
        inner = existing_metric.buffer(-float(settings.edge_buffer_m))
        old_edge_band = outer.difference(inner).buffer(0) if not outer.is_empty else GeometryCollection()

    kept_features: list[dict[str, Any]] = []
    rejected_features: list[dict[str, Any]] = []
    flagged_features: list[dict[str, Any]] = []
    diagnostic_features: list[dict[str, Any]] = []

    for feature in candidates_geojson.get("features", []):
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            continue
        try:
            geometry_wgs84 = shape(feature["geometry"]).buffer(0)
        except Exception:
            continue
        if geometry_wgs84.is_empty or geometry_wgs84.geom_type not in {"Polygon", "MultiPolygon"}:
            continue

        geometry_metric = reproject_geometry(geometry_wgs84, "EPSG:4326", metric_crs).buffer(0)
        if geometry_metric.is_empty:
            continue

        properties = dict(feature.get("properties") or {})
        area_m2 = float(geometry_metric.area)
        mean_probability = properties.get("mean_probability")
        max_probability = properties.get("max_probability")
        mean_probability = float(mean_probability) if mean_probability is not None else None
        max_probability = float(max_probability) if max_probability is not None else None
        existing_overlap_ratio = (
            _ratio(geometry_metric.intersection(existing_metric).area, area_m2)
            if not existing_metric.is_empty
            else 0.0
        )
        old_edge_overlap_ratio = (
            _ratio(geometry_metric.intersection(old_edge_band).area, area_m2)
            if not old_edge_band.is_empty
            else 0.0
        )
        thinness = _thinness_ratio(geometry_metric)
        is_small_thin = (
            area_m2 < float(settings.thin_artifact_max_area_m2)
            and thinness is not None
            and thinness < float(settings.thinness_min_ratio)
        )

        reject_reason: str | None = None
        review_flag: str | None = None
        if area_m2 < float(settings.min_area_m2):
            reject_reason = "below_min_area"
        elif existing_overlap_ratio >= float(settings.max_existing_overlap_ratio):
            reject_reason = "overlaps_existing_footprint"
        elif (
            is_small_thin
            and old_edge_overlap_ratio >= float(settings.max_edge_overlap_ratio)
            and mean_probability is not None
            and mean_probability < float(settings.thin_artifact_max_mean_probability)
        ):
            reject_reason = "thin_low_confidence_old_edge_artifact"
        elif is_small_thin and old_edge_overlap_ratio >= float(settings.max_edge_overlap_ratio) and mean_probability is None:
            review_flag = "probability_metric_unavailable"
        elif is_small_thin and old_edge_overlap_ratio >= float(settings.max_edge_overlap_ratio) and mean_probability is not None:
            if mean_probability >= float(settings.thin_artifact_max_mean_probability):
                review_flag = "small_thin_high_confidence"
            else:
                review_flag = "near_existing_edge_but_high_confidence"
        elif is_small_thin:
            review_flag = "small_thin_standalone"

        kept = reject_reason is None
        properties.update(
            {
                "area_m2": area_m2,
                "mean_probability": mean_probability,
                "max_probability": max_probability,
                "existing_overlap_ratio": existing_overlap_ratio,
                "old_edge_overlap_ratio": old_edge_overlap_ratio,
                "thinness_ratio": thinness,
                "kept": kept,
                "reject_reason": reject_reason,
                "review_flag": review_flag,
            }
        )
        output_feature = {"type": "Feature", "geometry": mapping(geometry_wgs84), "properties": properties}
        diagnostic_features.append(output_feature)
        if kept:
            kept_features.append(output_feature)
            if review_flag is not None:
                flagged_features.append(output_feature)
        else:
            rejected_features.append(output_feature)

    return AdditionCandidateFilterResult(
        kept_geojson={"type": "FeatureCollection", "features": kept_features},
        rejected_geojson={"type": "FeatureCollection", "features": rejected_features},
        flagged_geojson={"type": "FeatureCollection", "features": flagged_features},
        diagnostics_geojson={"type": "FeatureCollection", "features": diagnostic_features},
    )
