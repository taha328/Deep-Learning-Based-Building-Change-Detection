from __future__ import annotations

import json
from pathlib import Path
import re
import zipfile

import numpy as np
import pandas as pd
from PIL import Image
import rasterio
from pyproj import Transformer

from src.domain.artifact_manifest import build_manifest, iter_exportable_artifacts, write_manifest_atomic
from src.domain.stage_timing import StageTimingRecorder
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


def _fallback_exportable_paths(request_dir: Path) -> list[Path]:
    source_request_file_re = re.compile(r"^(t1|t2|source)_.+_z\d+(_valid_mask)?\.tif$")
    allowed_names = {
        "change_probability.tif",
        "t1_building_probability.tif",
        "t2_building_probability.tif",
        "t1_building_mask.tif",
        "t2_building_mask.tif",
        "new_building_mask.tif",
        "new_building_labels.tif",
        "building_change_mask.tif",
        "building_change_labels.tif",
        "segmentation_probability.tif",
        "segmentation_mask.tif",
        "segmentation_labels.tif",
        "new_buildings.csv",
        "new_buildings.geojson",
        "building_blocks.csv",
        "building_blocks.geojson",
        "building_change_polygons.csv",
        "building_change_polygons.geojson",
        "addition_candidate_diagnostics.csv",
        "addition_candidate_diagnostics.geojson",
        "rejected_addition_candidates.geojson",
        "flagged_addition_candidates.geojson",
        "building_change_blocks.csv",
        "building_change_blocks.geojson",
        "segmentation_polygons.csv",
        "segmentation_polygons.geojson",
        "wayback_pair_summary.csv",
        "summary.csv",
    }
    excluded_names = {
        "manifest.json",
        "manifest.json.tmp",
        "export_bundle.zip",
        "run_response.json",
    }
    paths: list[Path] = []
    for candidate in sorted(request_dir.iterdir()):
        if not candidate.is_file():
            continue
        name = candidate.name
        if name in excluded_names:
            continue
        if name.endswith(".zip"):
            continue
        if name in {"t1_wayback_rgb.tif", "t2_wayback_rgb.tif", "source_wayback_rgb.tif"}:
            continue
        if source_request_file_re.fullmatch(name):
            continue
        if name.endswith("_valid_mask.tif"):
            continue
        if name.startswith("t1_") and name.endswith("_valid_mask.tif"):
            continue
        if name.startswith("t2_") and name.endswith("_valid_mask.tif"):
            continue
        if name.startswith("bandon_input_") or name == "t1_invalid_mask_for_arosics.tif" or name == "t2_invalid_mask_for_arosics.tif":
            continue
        if name == "bandon_run":
            continue
        if name.endswith("_preview.png") or name in allowed_names:
            paths.append(candidate)
            continue
        if name.startswith("building_block_buffer_") or name.startswith("building_change_buffer_"):
            paths.append(candidate)
    return paths


def _runtime_root_for_request_dir(request_dir: Path) -> Path:
    return request_dir.resolve().parents[1]


def _assert_exportable_path_allowed(request_dir: Path, file_path: Path) -> None:
    runtime_root = _runtime_root_for_request_dir(request_dir)
    resolved = file_path.resolve()
    if runtime_root != resolved and runtime_root not in resolved.parents:
        raise ValueError(f"Refusing to export artifact outside runtime cache: {resolved}")


def write_run_manifest(
    result_dir: Path,
    artifacts: list[ArtifactEntry],
    *,
    extra_artifacts: list[dict[str, object]] | None = None,
) -> Path:
    manifest_artifacts = [artifact.model_dump(mode="json") for artifact in artifacts]
    if extra_artifacts:
        manifest_artifacts.extend(extra_artifacts)
    manifest = build_manifest(result_dir.name, result_dir, manifest_artifacts)
    return write_manifest_atomic(result_dir, manifest)


def create_export_bundle_from_manifest(request_dir: Path, *, force: bool = False) -> Path:
    request_dir = request_dir.resolve()
    bundle_path = request_dir / "export_bundle.zip"
    if bundle_path.exists() and not force:
        return bundle_path

    timing = StageTimingRecorder(run_id=request_dir.name, pipeline_kind="export")
    with timing.stage("total"):
        with timing.stage("export_manifest_read"):
            exportable_paths = iter_exportable_artifacts(request_dir)
        with timing.stage("export_artifact_selection", manifest_hit=bool(exportable_paths)):
            if not exportable_paths:
                exportable_paths = _fallback_exportable_paths(request_dir)
            if not exportable_paths:
                raise ValueError(f"No exportable final artifacts found in {request_dir}.")
            for file_path in exportable_paths:
                _assert_exportable_path_allowed(request_dir, file_path)
        with timing.stage("export_zip_write", artifact_count=len(exportable_paths)):
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zip_file:
                for file_path in exportable_paths:
                    zip_file.write(file_path, arcname=file_path.name)
    try:
        timing.write_timing_report(request_dir / "export_timing.json")
    except OSError:
        pass
    return bundle_path


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
) -> tuple[PreviewImages, list[ArtifactEntry], str | None, TabularMetrics]:
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

    write_run_manifest(result_dir, artifacts)
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
    return previews, artifacts, None, tables


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
    addition_candidate_diagnostics_geojson: dict | None = None,
    rejected_addition_candidates_geojson: dict | None = None,
    flagged_addition_candidates_geojson: dict | None = None,
) -> tuple[PreviewImages, list[ArtifactEntry], str | None, TabularMetrics]:
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
    diagnostics_geojson_path = None
    rejected_geojson_path = None
    flagged_geojson_path = None
    diagnostics_csv_path = None
    if addition_candidate_diagnostics_geojson is not None:
        diagnostics_geojson_path = write_geojson(
            result_dir / "addition_candidate_diagnostics.geojson",
            addition_candidate_diagnostics_geojson,
        )
        diagnostics_rows = [feature.get("properties", {}) for feature in addition_candidate_diagnostics_geojson.get("features", [])]
        diagnostics_csv_path = write_csv(result_dir / "addition_candidate_diagnostics.csv", pd.DataFrame(diagnostics_rows))
    if rejected_addition_candidates_geojson is not None:
        rejected_geojson_path = write_geojson(
            result_dir / "rejected_addition_candidates.geojson",
            rejected_addition_candidates_geojson,
        )
    if flagged_addition_candidates_geojson is not None:
        flagged_geojson_path = write_geojson(
            result_dir / "flagged_addition_candidates.geojson",
            flagged_addition_candidates_geojson,
        )
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
    if diagnostics_csv_path is not None:
        artifacts.append(
            ArtifactEntry(
                name="addition_candidate_diagnostics_csv",
                path=str(diagnostics_csv_path),
                media_type="text/csv",
                description="Per-candidate addition filter diagnostics",
            )
        )
    if diagnostics_geojson_path is not None:
        artifacts.append(
            ArtifactEntry(
                name="addition_candidate_diagnostics_geojson",
                path=str(diagnostics_geojson_path),
                media_type="application/geo+json",
                description="Per-candidate addition filter diagnostics",
            )
        )
    if rejected_geojson_path is not None:
        artifacts.append(
            ArtifactEntry(
                name="rejected_addition_candidates_geojson",
                path=str(rejected_geojson_path),
                media_type="application/geo+json",
                description="Rejected building-addition candidates",
            )
        )
    if flagged_geojson_path is not None:
        artifacts.append(
            ArtifactEntry(
                name="flagged_addition_candidates_geojson",
                path=str(flagged_geojson_path),
                media_type="application/geo+json",
                description="Building-addition candidates flagged for review",
            )
        )
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

    write_run_manifest(result_dir, artifacts)
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
    return previews, artifacts, None, tables


def export_segmentation_outputs(
    *,
    result_dir: Path,
    reference_raster_path: Path,
    source_rgb: np.ndarray,
    segmentation_prob: np.ndarray,
    segmentation_mask: np.ndarray,
    segmentation_labels: np.ndarray,
    segmentation_df: pd.DataFrame,
    segmentation_geojson: dict,
    summary_df: pd.DataFrame,
) -> tuple[PreviewImages, list[ArtifactEntry], str | None, TabularMetrics]:
    source_preview_path = _save_png(result_dir / "source_preview.png", source_rgb)
    probability_preview_path = _save_png(
        result_dir / "segmentation_probability_preview.png",
        probability_rgb(segmentation_prob),
    )
    overlay_preview_path = _save_png(
        result_dir / "segmentation_overlay_preview.png",
        blend_rgb_mask(source_rgb, segmentation_mask.astype(bool), color=(0, 255, 0), alpha=0.45),
    )

    source_raster_path = save_multiband_like(
        reference_raster_path,
        result_dir / "source_wayback_rgb.tif",
        source_rgb,
        "uint8",
    )
    probability_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "segmentation_probability.tif",
        segmentation_prob,
        "float32",
    )
    mask_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "segmentation_mask.tif",
        segmentation_mask.astype(np.uint8),
        "uint8",
    )
    labels_raster = save_single_band_like(
        reference_raster_path,
        result_dir / "segmentation_labels.tif",
        segmentation_labels.astype(np.uint16),
        "uint16",
    )

    segmentation_csv = write_csv(result_dir / "segmentation_polygons.csv", segmentation_df)
    segmentation_geojson_path = write_geojson(result_dir / "segmentation_polygons.geojson", segmentation_geojson)
    summary_csv = write_csv(result_dir / "summary.csv", summary_df)

    artifacts = [
        ArtifactEntry(name="source_preview_png", path=str(source_preview_path), media_type="image/png", description="Source RGB preview"),
        ArtifactEntry(
            name="segmentation_probability_preview_png",
            path=str(probability_preview_path),
            media_type="image/png",
            description="SAM3 segmentation score preview",
        ),
        ArtifactEntry(
            name="segmentation_overlay_preview_png",
            path=str(overlay_preview_path),
            media_type="image/png",
            description="SAM3 segmentation overlay preview",
        ),
        ArtifactEntry(name="source_wayback_rgb_tif", path=str(source_raster_path), media_type="image/tiff", description="Source RGB GeoTIFF"),
        ArtifactEntry(
            name="segmentation_probability_tif",
            path=str(probability_raster),
            media_type="image/tiff",
            description="SAM3 segmentation probability raster",
        ),
        ArtifactEntry(name="segmentation_mask_tif", path=str(mask_raster), media_type="image/tiff", description="SAM3 segmentation mask raster"),
        ArtifactEntry(
            name="segmentation_labels_tif",
            path=str(labels_raster),
            media_type="image/tiff",
            description="Connected-component labels for segmentation regions",
        ),
        ArtifactEntry(name="segmentation_polygons_csv", path=str(segmentation_csv), media_type="text/csv", description="Segmentation polygon metrics"),
        ArtifactEntry(
            name="segmentation_polygons_geojson",
            path=str(segmentation_geojson_path),
            media_type="application/geo+json",
            description="Segmentation polygons",
        ),
        ArtifactEntry(name="summary_csv", path=str(summary_csv), media_type="text/csv", description="Segmentation summary"),
    ]

    write_run_manifest(result_dir, artifacts)
    previews = PreviewImages(
        t2_preview_path=str(source_preview_path),
        change_probability_preview_path=str(probability_preview_path),
        change_overlay_preview_path=str(overlay_preview_path),
        **_get_raster_georeferencing(reference_raster_path),
    )
    tables = TabularMetrics(
        summary_rows=summary_df.to_dict(orient="records"),
        change_rows=segmentation_df.to_dict(orient="records"),
    )
    return previews, artifacts, None, tables
