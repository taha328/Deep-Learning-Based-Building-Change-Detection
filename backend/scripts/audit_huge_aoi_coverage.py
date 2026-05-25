from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.config import get_settings
from src.utils.geometry import geodesic_area_m2


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("huge_aoi_coverage_audit")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _project_dir(project_id: str) -> Path:
    settings = get_settings()
    return settings.temporal_projects_dir / project_id


def _request_dir(request_id: str) -> Path:
    settings = get_settings()
    return settings.request_cache_dir / request_id


def _geom_from_geojson(payload: dict[str, Any]) -> BaseGeometry:
    if payload.get("type") == "FeatureCollection":
        geoms = [shape(feature["geometry"]) for feature in payload.get("features", []) if feature.get("geometry")]
        return unary_union(geoms).buffer(0) if geoms else box(0, 0, 0, 0)
    if payload.get("type") == "Feature":
        return shape(payload["geometry"])
    return shape(payload)


def _aoi_geometry_3857(project_payload: dict[str, Any]) -> tuple[BaseGeometry, dict[str, Any]]:
    aoi = project_payload["aoi_geojson"]
    geom_3857 = transform_geom("EPSG:4326", "EPSG:3857", aoi)
    return _geom_from_geojson(geom_3857), geom_3857


def _window_valid_stats(path: Path, aoi_geom_3857: dict[str, Any] | None = None, *, nonzero: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    with rasterio.open(path) as src:
        bounds_wgs84 = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21) if src.crs else None
        pixel_area = abs(src.transform.a * src.transform.e)
        valid_pixels = 0
        nodata_pixels = 0
        aoi_pixels = 0
        for _, window in src.block_windows(1):
            if nonzero:
                arr = src.read(1, window=window, masked=False)
                valid = arr != 0
            elif src.count == 1 and path.name == "valid_mask.tif":
                arr = src.read(1, window=window, masked=False)
                valid = arr > 0
            else:
                valid = src.dataset_mask(window=window) > 0
            if aoi_geom_3857 is not None:
                win_transform = src.window_transform(window)
                inside = geometry_mask([aoi_geom_3857], out_shape=valid.shape, transform=win_transform, invert=True)
                aoi_pixels += int(inside.sum())
                valid = valid & inside
                nodata_pixels += int((~valid & inside).sum())
            else:
                nodata_pixels += int((~valid).sum())
            valid_pixels += int(valid.sum())
        aoi_area = aoi_pixels * pixel_area
        valid_area = valid_pixels * pixel_area
        return {
            "exists": True,
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "crs": str(src.crs) if src.crs else None,
            "width": src.width,
            "height": src.height,
            "transform": list(src.transform)[:6],
            "resolution_m": [abs(src.transform.a), abs(src.transform.e)],
            "bounds_epsg3857": [src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top],
            "bounds_wgs84": list(bounds_wgs84) if bounds_wgs84 else None,
            "nodata": src.nodata,
            "valid_pixel_count": valid_pixels,
            "nodata_pixel_count": nodata_pixels,
            "valid_area_m2": valid_area,
            "aoi_intersection_area_m2": aoi_area,
            "aoi_coverage_pct": (valid_area / aoi_area * 100.0) if aoi_area else None,
        }


def _feature_payload_from_geojsonl(path: Path) -> dict[str, Any]:
    features = []
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    features.append(json.loads(line))
    return {"type": "FeatureCollection", "features": features}


def _vector_stats(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "path": str(path)}
    payload = _feature_payload_from_geojsonl(path) if path.suffix == ".geojsonl" else _load_json(path)
    features = payload.get("features", []) if isinstance(payload, dict) else []
    geom = _geom_from_geojson(payload) if features else box(0, 0, 0, 0)
    prop_area = 0.0
    prop_seen = False
    for feature in features:
        props = feature.get("properties") or {}
        value = props.get("area_m2") or props.get("area_sqm") or props.get("area")
        if value is not None:
            try:
                prop_area += float(value)
                prop_seen = True
            except (TypeError, ValueError):
                pass
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "feature_count": len(features),
        "bounds_wgs84": list(geom.bounds) if not geom.is_empty else None,
        "geometry_area_m2": geodesic_area_m2(geom) if not geom.is_empty else 0.0,
        "property_area_m2": prop_area if prop_seen else None,
    }


def _tile_metadata(path: Path, aoi_bounds_3857: tuple[float, float, float, float]) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "path": str(path)}
    payload = _load_json(path)
    selected = payload.get("selected_tile_count") or payload.get("tile_count")
    available = payload.get("available_tile_count")
    missing = payload.get("missing_tile_count")
    failed = payload.get("failed_tile_count", 0)
    return {
        "exists": True,
        "path": str(path),
        "release": payload.get("release_identifier"),
        "effective_zoom": payload.get("zoom"),
        "selected_tile_count": selected,
        "downloaded_tile_count": available,
        "cache_hit_count": payload.get("cache_hit_count"),
        "missing_tile_count": missing,
        "failed_tile_count": failed,
        "tile_count": payload.get("tile_count"),
        "tile_range": payload.get("tile_range"),
        "bounds_3857": payload.get("bounds_3857"),
        "aoi_tile_coverage_pct": (float(available) / float(selected) * 100.0) if selected and available is not None else None,
        "aoi_bounds_3857": list(aoi_bounds_3857),
    }


def _prediction_window_stats(mask_path: Path, *, tile_size: int, stride: int) -> dict[str, int | None]:
    if not mask_path.is_file() or tile_size <= 0 or stride <= 0:
        return {
            "total_windows_recomputed": None,
            "nonzero_prediction_windows": None,
            "lower_aoi_windows": None,
            "lower_nonzero_prediction_windows": None,
        }
    with rasterio.open(mask_path) as src:
        y_positions = list(range(0, max(src.height - tile_size, 0) + 1, stride))
        if y_positions and y_positions[-1] != src.height - tile_size:
            y_positions.append(src.height - tile_size)
        x_positions = list(range(0, max(src.width - tile_size, 0) + 1, stride))
        if x_positions and x_positions[-1] != src.width - tile_size:
            x_positions.append(src.width - tile_size)
        total = 0
        nonzero = 0
        lower = 0
        lower_nonzero = 0
        for y in y_positions:
            for x in x_positions:
                total += 1
                window = rasterio.windows.Window(x, y, tile_size, tile_size)
                arr = src.read(1, window=window, boundless=False)
                has_prediction = bool((arr != 0).any())
                nonzero += int(has_prediction)
                if y + tile_size > src.height / 2:
                    lower += 1
                    lower_nonzero += int(has_prediction)
        return {
            "total_windows_recomputed": total,
            "nonzero_prediction_windows": nonzero,
            "lower_aoi_windows": lower,
            "lower_nonzero_prediction_windows": lower_nonzero,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit huge AOI raster/vector coverage without mutating project data.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--baseline-release", required=True)
    parser.add_argument("--target-release", required=True)
    args = parser.parse_args()

    logger.info("AOI_COVERAGE_AUDIT_START projectId=%s requestId=%s", args.project_id, args.request_id)
    project_dir = _project_dir(args.project_id)
    request_dir = _request_dir(args.request_id)
    project_payload = _load_json(project_dir / "project.json")
    aoi_3857, aoi_geom_payload = _aoi_geometry_3857(project_payload)
    aoi_bounds = aoi_3857.bounds
    aoi_area = aoi_3857.area

    tiled_metadata_path = request_dir / "tiled_inference_metadata.json"
    tiled_metadata = _load_json(tiled_metadata_path) if tiled_metadata_path.is_file() else {}
    source_t1 = Path(tiled_metadata.get("t1_mosaic_path") or "")
    source_t2 = Path(tiled_metadata.get("t2_mosaic_path") or "")

    rasters = {
        f"{args.baseline_release} reference_imagery_cog.tif": project_dir / "milestones" / args.baseline_release / "reference_imagery_cog.tif",
        f"{args.target_release} reference_imagery_cog.tif": project_dir / "milestones" / args.target_release / "reference_imagery_cog.tif",
        f"source {args.baseline_release} mosaic.tif": source_t1,
        f"source {args.target_release} mosaic.tif": source_t2,
        f"source {args.baseline_release} valid_mask.tif": source_t1.with_name("valid_mask.tif") if source_t1 else Path(),
        f"source {args.target_release} valid_mask.tif": source_t2.with_name("valid_mask.tif") if source_t2 else Path(),
        "prediction_change_mask.tif": request_dir / "prediction_change_mask.tif",
        "prediction_change_probability.tif": request_dir / "prediction_change_probability.tif",
    }
    raster_results = {}
    for name, path in rasters.items():
        stats = _window_valid_stats(path, aoi_geom_payload, nonzero=name == "prediction_change_mask.tif")
        raster_results[name] = stats
        logger.info("AOI_COVERAGE_AUDIT_RASTER %s", json.dumps({"artifact": name, **stats}, sort_keys=True))

    vectors = {
        "prediction_change_polygons.geojsonl": request_dir / "prediction_change_polygons.geojsonl",
        "building_change_polygons.geojson": request_dir / "building_change_polygons.geojson",
        "automated_additions.geojson": project_dir / "milestones" / args.target_release / "automated_additions.geojson",
        "additions.geojson": project_dir / "milestones" / args.target_release / "additions.geojson",
        "effective_footprint.geojson": project_dir / "milestones" / args.target_release / "effective_footprint.geojson",
        "cumulative_growth_blocks.geojson": project_dir / "milestones" / args.target_release / "cumulative_growth_blocks.geojson",
    }
    vector_results = {}
    for name, path in vectors.items():
        stats = _vector_stats(path)
        vector_results[name] = stats
        logger.info("AOI_COVERAGE_AUDIT_VECTOR_STAGE %s", json.dumps({"artifact": name, **stats}, sort_keys=True))

    tile_results = {}
    for label, mosaic in ((args.baseline_release, source_t1), (args.target_release, source_t2)):
        stats = _tile_metadata(mosaic.with_name("metadata.json"), aoi_bounds)
        tile_results[label] = stats
        logger.info("AOI_COVERAGE_AUDIT_TILE_SET %s", json.dumps(stats, sort_keys=True))

    mask_area_m2 = raster_results.get("prediction_change_mask.tif", {}).get("valid_area_m2")
    prediction_windows = _prediction_window_stats(
        request_dir / "prediction_change_mask.tif",
        tile_size=int(tiled_metadata.get("tile_size") or 0),
        stride=int(tiled_metadata.get("stride") or 0),
    )
    inference = {
        "total_windows": tiled_metadata.get("total_tiles"),
        "processed_windows": tiled_metadata.get("processed_tiles"),
        "skipped_windows": tiled_metadata.get("skipped_tiles_this_run"),
        "empty_windows": None,
        "low_valid_ratio_skips": None,
        "windows_with_nonzero_prediction": prediction_windows["nonzero_prediction_windows"],
        "windows_intersecting_aoi": tiled_metadata.get("selected_tiles"),
        "windows_covering_lower_aoi": "processed" if tiled_metadata.get("processed_tiles") == tiled_metadata.get("total_tiles") else "unknown",
        **prediction_windows,
    }
    logger.info("AOI_COVERAGE_AUDIT_INFERENCE_WINDOWS %s", json.dumps(inference, sort_keys=True))
    logger.info(
        "AOI_COVERAGE_AUDIT_METRIC %s",
        json.dumps(
            {
                "raw_mask_area_m2": mask_area_m2,
                "additions_area_m2": vector_results.get("additions.geojson", {}).get("geometry_area_m2"),
                "frontend_metric_m2": (project_payload.get("milestones") or [{}])[-1].get("metrics", {}).get("added_area_m2"),
            },
            sort_keys=True,
        ),
    )
    min_coverage = min(
        [
            value.get("aoi_coverage_pct")
            for key, value in raster_results.items()
            if key.endswith("reference_imagery_cog.tif") and value.get("aoi_coverage_pct") is not None
        ]
        or [0]
    )
    logger.info(
        "%s projectId=%s minReferenceCoveragePct=%s",
        "AOI_COVERAGE_COMPLETE" if min_coverage >= 99.0 else "AOI_COVERAGE_INCOMPLETE",
        args.project_id,
        round(float(min_coverage), 4),
    )
    print(
        json.dumps(
            {
                "project_id": args.project_id,
                "request_id": args.request_id,
                "aoi": {
                    "area_epsg3857_m2": aoi_area,
                    "bounds_epsg3857": list(aoi_bounds),
                    "bounds_wgs84": list(transform_bounds("EPSG:3857", "EPSG:4326", *aoi_bounds, densify_pts=21)),
                },
                "rasters": raster_results,
                "tiles": tile_results,
                "inference": inference,
                "vectors": vector_results,
                "metadata": {
                    "project_json_size_bytes": (project_dir / "project.json").stat().st_size,
                    "project_manifest_size_bytes": (project_dir / "project_manifest.json").stat().st_size,
                    "project_summary_size_bytes": (project_dir / "project_summary.json").stat().st_size,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
