from __future__ import annotations

import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
from PIL import Image
import rasterio
from pyproj import Transformer

from src.schemas import ArtifactEntry, PreviewImages, TabularMetrics
from src.utils.raster import save_multiband_like, save_single_band_like


def blend_rgb_mask(base_rgb: np.ndarray, mask: np.ndarray, *, color: tuple[int, int, int], alpha: float) -> np.ndarray:
    base = base_rgb.astype(np.float32).copy()
    color_arr = np.array(color, dtype=np.float32)
    base[mask] = (1.0 - alpha) * base[mask] + alpha * color_arr
    return np.clip(base, 0, 255).astype(np.uint8)


def probability_rgb(probability: np.ndarray) -> np.ndarray:
    clipped = np.clip(probability, 0.0, 1.0)
    red = (255.0 * clipped).astype(np.uint8)
    green = (180.0 * np.sqrt(clipped)).astype(np.uint8)
    blue = (120.0 * (1.0 - clipped)).astype(np.uint8)
    return np.stack([red, green, blue], axis=-1)


def _get_raster_bounds_wgs84(raster_path: Path) -> list[float] | None:
    """
    Extract geographic bounds from a GeoTIFF and convert to WGS84 (EPSG:4326).
    
    Returns:
        [minx, miny, maxx, maxy] in WGS84 degrees (longitude, latitude), or None if extraction fails
    """
    try:
        with rasterio.open(raster_path) as src:
            if src.bounds is None or src.crs is None:
                return None
            
            # Get bounds in the raster's native CRS
            minx, miny, maxx, maxy = src.bounds
            
            # If already in WGS84, return directly
            if src.crs.to_epsg() == 4326:
                return [float(minx), float(miny), float(maxx), float(maxy)]
            
            # Transform from the raster's CRS to WGS84
            transformer = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
            
            # Transform the bounding box corners
            min_lon, min_lat = transformer.transform(minx, miny)
            max_lon, max_lat = transformer.transform(maxx, maxy)
            
            return [float(min_lon), float(min_lat), float(max_lon), float(max_lat)]
    except Exception:
        return None


def _get_raster_georeferencing(raster_path: Path) -> dict[str, object]:
    try:
        with rasterio.open(raster_path) as src:
            if src.bounds is None or src.crs is None:
                return {}

            bounds_native = [
                float(src.bounds.left),
                float(src.bounds.bottom),
                float(src.bounds.right),
                float(src.bounds.top),
            ]
            bounds_wgs84 = _get_raster_bounds_wgs84(raster_path)
            transform = [
                float(src.transform.a),
                float(src.transform.b),
                float(src.transform.c),
                float(src.transform.d),
                float(src.transform.e),
                float(src.transform.f),
            ]

            return {
                "raster_bounds_wgs84": bounds_wgs84,
                "raster_bounds_native": bounds_native,
                "raster_crs": str(src.crs),
                "raster_transform": transform,
                "raster_size": [int(src.width), int(src.height)],
            }
    except Exception:
        return {}


def _save_png(path: Path, array: np.ndarray) -> Path:
    Image.fromarray(array).save(path)
    return path


def write_geojson(path: Path, feature_collection: dict) -> Path:
    path.write_text(json.dumps(feature_collection, indent=2))
    return path


def write_csv(path: Path, dataframe: pd.DataFrame) -> Path:
    dataframe.to_csv(path, index=False)
    return path


def export_run_outputs(
    *,
    result_dir: Path,
    reference_raster_path: Path,
    t1_rgb: np.ndarray,
    t2_rgb: np.ndarray,
    change_prob: np.ndarray,
    t1_building_prob: np.ndarray,
    t2_building_prob: np.ndarray,
    t1_building_mask: np.ndarray,
    t2_building_mask: np.ndarray,
    new_building_mask: np.ndarray,
    new_building_labels: np.ndarray,
    new_buildings_df: pd.DataFrame,
    new_buildings_geojson: dict,
    building_blocks_df: pd.DataFrame,
    building_blocks_geojson: dict,
    buffer_layers: dict[str, tuple[pd.DataFrame, dict]],
    summary_df: pd.DataFrame,
) -> tuple[PreviewImages, list[ArtifactEntry], str, TabularMetrics]:
    t1_preview_path = _save_png(result_dir / "t1_preview.png", t1_rgb)
    t2_preview_path = _save_png(result_dir / "t2_preview.png", t2_rgb)
    change_prob_preview_path = _save_png(result_dir / "change_probability_preview.png", probability_rgb(change_prob))
    change_overlay_preview_path = _save_png(
        result_dir / "change_overlay_preview.png",
        blend_rgb_mask(t2_rgb, new_building_mask, color=(0, 255, 0), alpha=0.55),
    )

    t1_raster_path = save_multiband_like(reference_raster_path, result_dir / "t1_wayback_rgb.tif", t1_rgb, "uint8")
    t2_raster_path = save_multiband_like(reference_raster_path, result_dir / "t2_wayback_rgb.tif", t2_rgb, "uint8")
    change_prob_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "change_probability.tif",
        change_prob,
        "float32",
    )
    t1_prob_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "t1_building_probability.tif",
        t1_building_prob,
        "float32",
    )
    t2_prob_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "t2_building_probability.tif",
        t2_building_prob,
        "float32",
    )
    t1_mask_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "t1_building_mask.tif",
        t1_building_mask.astype(np.uint8),
        "uint8",
    )
    t2_mask_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "t2_building_mask.tif",
        t2_building_mask.astype(np.uint8),
        "uint8",
    )
    mask_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "new_building_mask.tif",
        new_building_mask.astype(np.uint8),
        "uint8",
    )
    labels_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "new_building_labels.tif",
        new_building_labels.astype(np.uint16),
        "uint16",
    )

    new_buildings_csv = write_csv(result_dir / "new_buildings.csv", new_buildings_df)
    new_buildings_geojson_path = write_geojson(result_dir / "new_buildings.geojson", new_buildings_geojson)
    building_blocks_csv = write_csv(result_dir / "building_blocks.csv", building_blocks_df)
    building_blocks_geojson_path = write_geojson(result_dir / "building_blocks.geojson", building_blocks_geojson)
    summary_csv = write_csv(result_dir / "wayback_pair_summary.csv", summary_df)

    artifacts = [
        ArtifactEntry(name="t1_preview_png", path=str(t1_preview_path), media_type="image/png", description="T1 RGB preview"),
        ArtifactEntry(name="t2_preview_png", path=str(t2_preview_path), media_type="image/png", description="T2 RGB preview"),
        ArtifactEntry(
            name="change_probability_preview_png",
            path=str(change_prob_preview_path),
            media_type="image/png",
            description="SAM3-derived change score preview",
        ),
        ArtifactEntry(
            name="change_overlay_preview_png",
            path=str(change_overlay_preview_path),
            media_type="image/png",
            description="New-building overlay preview",
        ),
        ArtifactEntry(name="t1_wayback_rgb_tif", path=str(t1_raster_path), media_type="image/tiff", description="T1 RGB GeoTIFF"),
        ArtifactEntry(name="t2_wayback_rgb_tif", path=str(t2_raster_path), media_type="image/tiff", description="T2 RGB GeoTIFF"),
        ArtifactEntry(
            name="change_probability_tif",
            path=str(change_prob_raster),
            media_type="image/tiff",
            description="SAM3-derived change score raster",
        ),
        ArtifactEntry(
            name="t1_building_probability_tif",
            path=str(t1_prob_raster),
            media_type="image/tiff",
            description="T1 building score raster",
        ),
        ArtifactEntry(
            name="t2_building_probability_tif",
            path=str(t2_prob_raster),
            media_type="image/tiff",
            description="T2 building score raster",
        ),
        ArtifactEntry(name="t1_building_mask_tif", path=str(t1_mask_raster), media_type="image/tiff", description="T1 building mask raster"),
        ArtifactEntry(name="t2_building_mask_tif", path=str(t2_mask_raster), media_type="image/tiff", description="T2 building mask raster"),
        ArtifactEntry(name="new_building_mask_tif", path=str(mask_raster), media_type="image/tiff", description="New building mask raster"),
        ArtifactEntry(name="new_building_labels_tif", path=str(labels_raster), media_type="image/tiff", description="Connected-component labels raster"),
        ArtifactEntry(name="new_buildings_csv", path=str(new_buildings_csv), media_type="text/csv", description="New building metrics"),
        ArtifactEntry(name="new_buildings_geojson", path=str(new_buildings_geojson_path), media_type="application/geo+json", description="New building polygons"),
        ArtifactEntry(name="building_blocks_csv", path=str(building_blocks_csv), media_type="text/csv", description="Building block metrics"),
        ArtifactEntry(
            name="building_blocks_geojson",
            path=str(building_blocks_geojson_path),
            media_type="application/geo+json",
            description="Building block polygons",
        ),
        ArtifactEntry(name="summary_csv", path=str(summary_csv), media_type="text/csv", description="Pair summary"),
    ]

    buffer_rows: dict[str, list[dict]] = {}
    for label, (buffer_df, buffer_geojson) in buffer_layers.items():
        buffer_csv = write_csv(result_dir / f"building_block_buffer_{label}.csv", buffer_df)
        buffer_geojson_path = write_geojson(result_dir / f"building_block_buffer_{label}.geojson", buffer_geojson)
        artifacts.append(
            ArtifactEntry(
                name=f"building_block_buffer_{label}_csv",
                path=str(buffer_csv),
                media_type="text/csv",
                description=f"Building block buffer metrics for {label}",
            )
        )
        artifacts.append(
            ArtifactEntry(
                name=f"building_block_buffer_{label}_geojson",
                path=str(buffer_geojson_path),
                media_type="application/geo+json",
                description=f"Building block buffers for {label}",
            )
        )
        buffer_rows[label] = buffer_df.to_dict(orient="records")

    bundle_path = result_dir / "export_bundle.zip"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in sorted(result_dir.glob("*")):
            if file_path.is_file() and file_path.name != bundle_path.name:
                zip_file.write(file_path, arcname=file_path.name)

    artifacts.append(
        ArtifactEntry(
            name="export_bundle_zip",
            path=str(bundle_path),
            media_type="application/zip",
            description="Complete export bundle",
        )
    )

    previews = PreviewImages(
        t1_preview_path=str(t1_preview_path),
        t2_preview_path=str(t2_preview_path),
        change_probability_preview_path=str(change_prob_preview_path),
        change_overlay_preview_path=str(change_overlay_preview_path),
        **_get_raster_georeferencing(reference_raster_path),
    )
    tables = TabularMetrics(
        summary_rows=summary_df.to_dict(orient="records"),
        new_building_rows=new_buildings_df.to_dict(orient="records"),
        building_block_rows=building_blocks_df.to_dict(orient="records"),
        buffer_rows=buffer_rows,
    )
    return previews, artifacts, str(bundle_path), tables


def export_bandon_outputs(
    *,
    result_dir: Path,
    reference_raster_path: Path,
    t1_rgb: np.ndarray,
    t2_rgb: np.ndarray,
    change_prob: np.ndarray,
    change_mask: np.ndarray,
    change_labels: np.ndarray,
    change_polygons_df: pd.DataFrame,
    change_polygons_geojson: dict,
    change_blocks_df: pd.DataFrame,
    change_blocks_geojson: dict,
    buffer_layers: dict[str, tuple[pd.DataFrame, dict]],
    summary_df: pd.DataFrame,
    bandon_metadata_path: Path | None = None,
) -> tuple[PreviewImages, list[ArtifactEntry], str, TabularMetrics]:
    t1_preview_path = _save_png(result_dir / "t1_preview.png", t1_rgb)
    t2_preview_path = _save_png(result_dir / "t2_preview.png", t2_rgb)
    change_prob_preview_path = _save_png(result_dir / "change_probability_preview.png", probability_rgb(change_prob))
    change_overlay_preview_path = _save_png(
        result_dir / "change_overlay_preview.png",
        blend_rgb_mask(t2_rgb, change_mask.astype(bool), color=(255, 0, 0), alpha=0.45),
    )

    t1_raster_path = save_multiband_like(reference_raster_path, result_dir / "t1_wayback_rgb.tif", t1_rgb, "uint8")
    t2_raster_path = save_multiband_like(reference_raster_path, result_dir / "t2_wayback_rgb.tif", t2_rgb, "uint8")
    change_prob_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "change_probability.tif",
        change_prob,
        "float32",
    )
    change_mask_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "building_change_mask.tif",
        change_mask.astype(np.uint8),
        "uint8",
    )
    change_labels_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "building_change_labels.tif",
        change_labels.astype(np.uint16),
        "uint16",
    )

    change_polygons_csv = write_csv(result_dir / "building_change_polygons.csv", change_polygons_df)
    change_polygons_geojson_path = write_geojson(result_dir / "building_change_polygons.geojson", change_polygons_geojson)
    change_blocks_csv = write_csv(result_dir / "building_change_blocks.csv", change_blocks_df)
    change_blocks_geojson_path = write_geojson(result_dir / "building_change_blocks.geojson", change_blocks_geojson)
    summary_csv = write_csv(result_dir / "wayback_pair_summary.csv", summary_df)

    artifacts = [
        ArtifactEntry(name="t1_preview_png", path=str(t1_preview_path), media_type="image/png", description="T1 RGB preview"),
        ArtifactEntry(name="t2_preview_png", path=str(t2_preview_path), media_type="image/png", description="T2 RGB preview"),
        ArtifactEntry(
            name="change_probability_preview_png",
            path=str(change_prob_preview_path),
            media_type="image/png",
            description="BANDON change score preview",
        ),
        ArtifactEntry(
            name="change_overlay_preview_png",
            path=str(change_overlay_preview_path),
            media_type="image/png",
            description="Building-change overlay preview",
        ),
        ArtifactEntry(name="t1_wayback_rgb_tif", path=str(t1_raster_path), media_type="image/tiff", description="T1 RGB GeoTIFF"),
        ArtifactEntry(name="t2_wayback_rgb_tif", path=str(t2_raster_path), media_type="image/tiff", description="T2 RGB GeoTIFF"),
        ArtifactEntry(
            name="change_probability_tif",
            path=str(change_prob_raster),
            media_type="image/tiff",
            description="BANDON change probability raster",
        ),
        ArtifactEntry(
            name="building_change_mask_tif",
            path=str(change_mask_raster),
            media_type="image/tiff",
            description="BANDON building-change mask raster",
        ),
        ArtifactEntry(
            name="building_change_labels_tif",
            path=str(change_labels_raster),
            media_type="image/tiff",
            description="Connected-component labels for building-change regions",
        ),
        ArtifactEntry(
            name="building_change_polygons_csv",
            path=str(change_polygons_csv),
            media_type="text/csv",
            description="Building-change polygon metrics",
        ),
        ArtifactEntry(
            name="building_change_polygons_geojson",
            path=str(change_polygons_geojson_path),
            media_type="application/geo+json",
            description="Building-change polygons",
        ),
        ArtifactEntry(
            name="building_change_blocks_csv",
            path=str(change_blocks_csv),
            media_type="text/csv",
            description="Building-change block metrics",
        ),
        ArtifactEntry(
            name="building_change_blocks_geojson",
            path=str(change_blocks_geojson_path),
            media_type="application/geo+json",
            description="Building-change blocks",
        ),
        ArtifactEntry(name="summary_csv", path=str(summary_csv), media_type="text/csv", description="Pair summary"),
    ]
    buffer_rows: dict[str, list[dict]] = {}
    for label, (buffer_df, buffer_geojson) in buffer_layers.items():
        buffer_csv = write_csv(result_dir / f"building_change_buffer_{label}.csv", buffer_df)
        buffer_geojson_path = write_geojson(result_dir / f"building_change_buffer_{label}.geojson", buffer_geojson)
        artifacts.append(
            ArtifactEntry(
                name=f"building_change_buffer_{label}_csv",
                path=str(buffer_csv),
                media_type="text/csv",
                description=f"Building-change buffer metrics for {label}",
            )
        )
        artifacts.append(
            ArtifactEntry(
                name=f"building_change_buffer_{label}_geojson",
                path=str(buffer_geojson_path),
                media_type="application/geo+json",
                description=f"Building-change buffers for {label}",
            )
        )
        buffer_rows[label] = buffer_df.to_dict(orient="records")

    if bandon_metadata_path is not None and bandon_metadata_path.exists():
        artifacts.append(
            ArtifactEntry(
                name="bandon_run_metadata_json",
                path=str(bandon_metadata_path),
                media_type="application/json",
                description="Raw BANDON MTGCDNet run metadata",
            )
        )

    bundle_path = result_dir / "export_bundle.zip"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in sorted(result_dir.glob("*")):
            if file_path.is_file() and file_path.name != bundle_path.name:
                zip_file.write(file_path, arcname=file_path.name)

    artifacts.append(
        ArtifactEntry(
            name="export_bundle_zip",
            path=str(bundle_path),
            media_type="application/zip",
            description="Complete export bundle",
        )
    )

    previews = PreviewImages(
        t1_preview_path=str(t1_preview_path),
        t2_preview_path=str(t2_preview_path),
        change_probability_preview_path=str(change_prob_preview_path),
        change_overlay_preview_path=str(change_overlay_preview_path),
        **_get_raster_georeferencing(reference_raster_path),
    )
    tables = TabularMetrics(
        summary_rows=summary_df.to_dict(orient="records"),
        change_rows=change_polygons_df.to_dict(orient="records"),
        buffer_rows=buffer_rows,
    )
    return previews, artifacts, str(bundle_path), tables
