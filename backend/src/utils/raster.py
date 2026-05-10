from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import reproject

from src.utils.geometry import parse_aoi_geometry, reproject_geometry


def read_rgb(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        bands = [src.read(index) for index in range(1, min(src.count, 3) + 1)]
    return np.stack(bands, axis=-1)


def save_single_band_like(reference_path: Path, output_path: Path, array: np.ndarray, dtype: str) -> Path:
    with rasterio.open(reference_path) as src:
        profile = src.profile.copy()
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    profile.pop("tiled", None)
    profile.update(driver="GTiff", count=1, dtype=dtype)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(array.astype(dtype), 1)
    return output_path


def save_multiband_like(
    reference_path: Path,
    output_path: Path,
    array: np.ndarray,
    dtype: str,
    compress: str = "LZW",
) -> Path:
    with rasterio.open(reference_path) as src:
        profile = src.profile.copy()
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    profile.pop("tiled", None)
    if array.ndim == 2:
        array = array[:, :, None]
    profile.update(driver="GTiff", count=array.shape[2], dtype=dtype, compress=compress)
    with rasterio.open(output_path, "w", **profile) as dst:
        for band_idx in range(array.shape[2]):
            dst.write(array[:, :, band_idx].astype(dtype), band_idx + 1)
    return output_path


def align_rgb_to_reference(source_path: Path, reference_path: Path) -> np.ndarray:
    with rasterio.open(source_path) as src, rasterio.open(reference_path) as ref:
        destination = np.zeros((src.count, ref.height, ref.width), dtype=np.float32)
        for band_idx in range(1, src.count + 1):
            reproject(
                source=src.read(band_idx),
                destination=destination[band_idx - 1],
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=ref.transform,
                dst_crs=ref.crs,
                resampling=Resampling.bilinear,
            )
    return np.transpose(destination, (1, 2, 0)).clip(0, 255).astype(np.uint8)


def align_single_band_mask_to_reference(source_path: Path, reference_path: Path) -> np.ndarray:
    with rasterio.open(source_path) as src, rasterio.open(reference_path) as ref:
        destination = np.zeros((ref.height, ref.width), dtype=np.float32)
        reproject(
            source=src.read(1),
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref.transform,
            dst_crs=ref.crs,
            resampling=Resampling.nearest,
        )
    return destination > 0.5


def rasterize_aoi_mask_like(reference_path: Path, aoi_geojson: dict[str, Any]) -> np.ndarray:
    with rasterio.open(reference_path) as ref:
        if ref.crs is None:
            raise ValueError(f"Reference raster has no CRS: {reference_path}")
        geometry = parse_aoi_geometry(aoi_geojson)
        reference_crs = ref.crs.to_string()
        if reference_crs != "EPSG:4326":
            geometry = reproject_geometry(geometry, "EPSG:4326", reference_crs)
        mask = rasterize(
            [(geometry, 1)],
            out_shape=(ref.height, ref.width),
            transform=ref.transform,
            fill=0,
            dtype="uint8",
            all_touched=False,
        )
    return mask.astype(bool)
