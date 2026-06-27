from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window


BIGTIFF_THRESHOLD_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_RASTER_BLOCK_SIZE = 512


def raster_uncompressed_size_bytes(
    *,
    width: int,
    height: int,
    band_count: int,
    dtype: object,
) -> int:
    if width < 1 or height < 1 or band_count < 1:
        raise ValueError("Raster dimensions and band count must be positive.")
    return int(width) * int(height) * int(band_count) * int(np.dtype(dtype).itemsize)


def raster_block_size(size: int, *, preferred: int = DEFAULT_RASTER_BLOCK_SIZE) -> int:
    if size >= preferred:
        return preferred
    if size >= 256:
        return 256
    return max(16, ((size + 15) // 16) * 16)


def bigtiff_policy_for_size(
    estimated_uncompressed_bytes: int,
    *,
    force: bool = False,
    threshold_bytes: int = BIGTIFF_THRESHOLD_BYTES,
) -> str:
    if force or estimated_uncompressed_bytes >= threshold_bytes:
        return "YES"
    return "IF_SAFER"


def large_geotiff_creation_options(
    *,
    width: int,
    height: int,
    band_count: int,
    dtype: object,
    compression: str,
    force_bigtiff: bool = False,
    predictor: int | None = None,
    block_size: int = DEFAULT_RASTER_BLOCK_SIZE,
) -> tuple[dict[str, Any], int]:
    estimated_bytes = raster_uncompressed_size_bytes(
        width=width,
        height=height,
        band_count=band_count,
        dtype=dtype,
    )
    options: dict[str, Any] = {
        "compress": compression,
        "tiled": True,
        "blockxsize": raster_block_size(width, preferred=block_size),
        "blockysize": raster_block_size(height, preferred=block_size),
        "BIGTIFF": bigtiff_policy_for_size(estimated_bytes, force=force_bigtiff),
    }
    if predictor is not None:
        options["predictor"] = predictor
    return options, estimated_bytes


def validate_geotiff_file(
    path: Path,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    min_band_count: int,
    require_crs: bool = True,
) -> dict[str, object]:
    with rasterio.open(path) as src:
        if expected_width is not None and expected_height is not None and (
            src.width != expected_width or src.height != expected_height
        ):
            raise ValueError(
                f"Raster dimensions mismatch for {path}: "
                f"{src.width}x{src.height}, expected {expected_width}x{expected_height}."
            )
        if src.count < min_band_count:
            raise ValueError(f"Raster band count mismatch for {path}: {src.count}, expected at least {min_band_count}.")
        if require_crs and src.crs is None:
            raise ValueError(f"Raster has no CRS: {path}")
        src.read(1, window=Window(0, 0, min(1, src.width), min(1, src.height)))
        return {
            "width": src.width,
            "height": src.height,
            "band_count": src.count,
            "crs": src.crs.to_string() if src.crs else None,
            "driver": src.driver,
            "compression": src.compression.value if src.compression else None,
            "is_tiled": bool(src.profile.get("tiled", False)),
            "block_shapes": [list(shape) for shape in src.block_shapes],
        }
