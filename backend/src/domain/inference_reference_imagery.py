from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import rasterio
from affine import Affine

from src.config import Settings
from src.domain.mosaic import MosaicResult, download_wayback_mosaic
from src.domain.reference_imagery_cache import (
    build_aoi_hash,
    build_reference_imagery_cache_key_payload,
    build_reference_imagery_cache_metadata,
    build_reference_imagery_key,
    read_reference_imagery_cache_metadata,
    reference_imagery_cache_cog_path,
    reference_imagery_cache_metadata_path,
    write_reference_imagery_cache_metadata,
)
from src.domain.tiling import tile_bounds_3857, tile_range_for_bbox
from src.domain.wayback import WaybackRelease
from src.services.temporal_reference_imagery import REFERENCE_COG_FORMAT_VERSION, ensure_reference_imagery_cog
from src.utils.raster import rasterize_aoi_mask_like

LOGGER = logging.getLogger(__name__)

_SUPPORTED_DTYPES = {"uint8", "uint16", "int16", "float32"}
_VALID_MASK_FILENAME = "valid_mask.tif"
_INFERENCE_REFERENCE_RESOLVER_VERSION = 1


@dataclass(frozen=True)
class CanonicalCogValidation:
    valid: bool
    reason: str | None
    diagnostics: dict[str, object]
    valid_mask_path: Path | None = None
    valid_mask_source: str | None = None


def _normal_tile_range(tile_range: tuple[int, int, int, int]) -> list[int]:
    return [int(value) for value in tile_range]


def _normal_bounds(bounds: tuple[float, float, float, float]) -> list[float]:
    return [float(value) for value in bounds]


def _tile_grid_for_bbox(bbox: dict[str, float], zoom: int) -> tuple[tuple[int, int, int, int], tuple[float, float, float, float], int, int, int]:
    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
    tile_range = (x_min, x_max, y_min, y_max)
    width = (x_max - x_min + 1) * 256
    height = (y_max - y_min + 1) * 256
    left, _, _, top = tile_bounds_3857(x_min, y_min, zoom)
    _, bottom, right, _ = tile_bounds_3857(x_max, y_max, zoom)
    return tile_range, (left, bottom, right, top), width, height, (x_max - x_min + 1) * (y_max - y_min + 1)


def _reference_key_payload(
    *,
    release: WaybackRelease,
    normalized_aoi: dict[str, Any],
    settings: Settings,
    zoom: int,
    tile_range: tuple[int, int, int, int],
    bounds_3857: tuple[float, float, float, float],
) -> dict[str, object]:
    return build_reference_imagery_cache_key_payload(
        provider="esri_wayback",
        release_identifier=release.identifier,
        release_num=release.release_num,
        tile_matrix_set=settings.tile_matrix_set,
        zoom=zoom,
        tile_range=_normal_tile_range(tile_range),
        bounds_3857=_normal_bounds(bounds_3857),
        source_raster_path=None,
        valid_mask_path=None,
        aoi_hash=build_aoi_hash(normalized_aoi),
        reference_cog_format_version=REFERENCE_COG_FORMAT_VERSION,
    )


def _same_sequence(left: object, right: list[int] | list[float], *, tolerance: float = 0.0) -> bool:
    if not isinstance(left, list) or len(left) != len(right):
        return False
    for actual, expected in zip(left, right):
        if not isinstance(actual, (int, float)):
            return False
        if tolerance:
            if abs(float(actual) - float(expected)) > tolerance:
                return False
        elif actual != expected:
            return False
    return True


def _expected_dimensions_from_payload(payload: dict[str, object]) -> tuple[int | None, int | None]:
    tile_range = payload.get("tile_range")
    if not (isinstance(tile_range, list) and len(tile_range) == 4 and all(isinstance(value, int) for value in tile_range)):
        return None, None
    x_min, x_max, y_min, y_max = tile_range
    return (int(x_max - x_min + 1) * 256, int(y_max - y_min + 1) * 256)


def _write_valid_mask_from_cog(cog_path: Path, valid_mask_path: Path) -> tuple[Path, str]:
    valid_mask_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = valid_mask_path.with_suffix(valid_mask_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    try:
        with rasterio.open(cog_path) as src:
            colorinterp = tuple(item.name for item in src.colorinterp)
            has_alpha = "alpha" in colorinterp and src.count >= 4
            has_internal_mask = any("per_dataset" in [flag.name for flag in flags] for flags in src.mask_flag_enums)
            if not has_alpha and not has_internal_mask:
                raise ValueError("canonical COG has no alpha band or internal mask")
            profile = src.profile.copy()
            profile.update(
                driver="GTiff",
                count=1,
                dtype="uint8",
                nodata=0,
                compress="LZW",
                tiled=bool(src.profile.get("tiled", True)),
                blockxsize=min(512, max(16, src.block_shapes[0][1] if src.block_shapes else 256)),
                blockysize=min(512, max(16, src.block_shapes[0][0] if src.block_shapes else 256)),
            )
            with rasterio.open(temp_path, "w", **profile) as dst:
                for _index, window in src.block_windows(1):
                    if has_alpha:
                        alpha_band = colorinterp.index("alpha") + 1
                        mask = src.read(alpha_band, window=window)
                        source = "alpha_band"
                    else:
                        mask = src.dataset_mask(window=window)
                        source = "internal_mask"
                    dst.write(np.where(mask > 0, 1, 0).astype(np.uint8), 1, window=window)
        temp_path.replace(valid_mask_path)
        return valid_mask_path, source
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _validate_valid_mask(mask_path: Path, *, cog_path: Path) -> bool:
    if not mask_path.is_file() or mask_path.stat().st_size <= 0:
        return False
    with rasterio.open(cog_path) as cog, rasterio.open(mask_path) as mask:
        return (
            mask.count >= 1
            and mask.width == cog.width
            and mask.height == cog.height
            and mask.crs == cog.crs
            and mask.transform == cog.transform
        )


def validate_canonical_cog_for_inference(
    *,
    canonical_cog_path: Path,
    metadata_path: Path,
    expected_reference_imagery_key: str,
    expected_key_payload: dict[str, object],
    normalized_aoi: dict[str, Any] | None,
) -> CanonicalCogValidation:
    diagnostics: dict[str, object] = {
        "canonical_cog_path": str(canonical_cog_path),
        "metadata_path": str(metadata_path),
        "expected_reference_imagery_key": expected_reference_imagery_key,
    }
    if not canonical_cog_path.is_file():
        return CanonicalCogValidation(False, "missing_canonical_cog", diagnostics)
    if canonical_cog_path.stat().st_size <= 0:
        return CanonicalCogValidation(False, "empty_canonical_cog", diagnostics)
    metadata = read_reference_imagery_cache_metadata(metadata_path)
    if metadata is None:
        return CanonicalCogValidation(False, "missing_metadata", diagnostics)
    diagnostics["metadata_reference_imagery_key"] = metadata.get("reference_imagery_key")
    if metadata.get("reference_imagery_key") != expected_reference_imagery_key:
        return CanonicalCogValidation(False, "reference_imagery_key_mismatch", diagnostics)
    for field in ("provider", "release_identifier", "release_num", "tile_matrix_set", "zoom", "aoi_hash"):
        if metadata.get(field) != expected_key_payload.get(field):
            diagnostics["mismatch_field"] = field
            diagnostics["actual"] = metadata.get(field)
            diagnostics["expected"] = expected_key_payload.get(field)
            return CanonicalCogValidation(False, f"{field}_mismatch", diagnostics)
    if not _same_sequence(metadata.get("tile_range"), expected_key_payload["tile_range"]):  # type: ignore[index]
        return CanonicalCogValidation(False, "tile_range_mismatch", diagnostics)
    if not _same_sequence(metadata.get("bounds_3857"), expected_key_payload["bounds_3857"], tolerance=1e-3):  # type: ignore[index]
        return CanonicalCogValidation(False, "bounds_mismatch", diagnostics)

    try:
        with rasterio.open(canonical_cog_path) as src:
            diagnostics.update(
                {
                    "crs": str(src.crs) if src.crs else None,
                    "transform": tuple(src.transform),
                    "width": src.width,
                    "height": src.height,
                    "bounds": tuple(float(value) for value in src.bounds),
                    "count": src.count,
                    "dtype": src.dtypes[0] if src.dtypes else None,
                    "colorinterp": [item.name for item in src.colorinterp],
                    "tiled": bool(src.profile.get("tiled")),
                    "has_internal_mask": any("per_dataset" in [flag.name for flag in flags] for flags in src.mask_flag_enums),
                }
            )
            if src.crs is None:
                return CanonicalCogValidation(False, "missing_crs", diagnostics)
            if src.transform == Affine.identity():
                return CanonicalCogValidation(False, "identity_transform", diagnostics)
            if src.width <= 0 or src.height <= 0:
                return CanonicalCogValidation(False, "invalid_dimensions", diagnostics)
            if not all(math.isfinite(value) for value in src.bounds):
                return CanonicalCogValidation(False, "invalid_bounds", diagnostics)
            expected_width, expected_height = _expected_dimensions_from_payload(expected_key_payload)
            if expected_width is not None and expected_height is not None and (src.width != expected_width or src.height != expected_height):
                return CanonicalCogValidation(False, "grid_dimension_mismatch", diagnostics)
            if not _same_sequence(
                [float(src.bounds.left), float(src.bounds.bottom), float(src.bounds.right), float(src.bounds.top)],
                expected_key_payload["bounds_3857"],  # type: ignore[index]
                tolerance=1e-3,
            ):
                return CanonicalCogValidation(False, "grid_bounds_mismatch", diagnostics)
            if src.count < 3:
                return CanonicalCogValidation(False, "insufficient_bands", diagnostics)
            if any(dtype not in _SUPPORTED_DTYPES for dtype in src.dtypes[:3]):
                return CanonicalCogValidation(False, "unsupported_dtype", diagnostics)
            if normalized_aoi is not None:
                aoi_mask = rasterize_aoi_mask_like(canonical_cog_path, normalized_aoi)
                diagnostics["aoi_mask_shape"] = tuple(int(value) for value in aoi_mask.shape)
                diagnostics["aoi_mask_valid_pixels"] = int(np.asarray(aoi_mask, dtype=bool).sum())
                if aoi_mask.shape != (src.height, src.width):
                    return CanonicalCogValidation(False, "aoi_mask_shape_mismatch", diagnostics)
    except Exception as exc:  # noqa: BLE001 - validation must downgrade to fallback with exact reason.
        diagnostics["exception"] = f"{type(exc).__name__}: {exc}"
        return CanonicalCogValidation(False, "read_failed", diagnostics)

    valid_mask_path = canonical_cog_path.with_name(_VALID_MASK_FILENAME)
    valid_mask_source = "valid_mask_tif"
    try:
        if not _validate_valid_mask(valid_mask_path, cog_path=canonical_cog_path):
            valid_mask_path, valid_mask_source = _write_valid_mask_from_cog(canonical_cog_path, valid_mask_path)
        if not _validate_valid_mask(valid_mask_path, cog_path=canonical_cog_path):
            return CanonicalCogValidation(False, "missing_valid_mask", diagnostics)
    except Exception as exc:  # noqa: BLE001
        diagnostics["valid_mask_error"] = f"{type(exc).__name__}: {exc}"
        return CanonicalCogValidation(False, "missing_valid_mask", diagnostics)

    diagnostics["valid_mask_path"] = str(valid_mask_path)
    diagnostics["valid_mask_source"] = valid_mask_source
    return CanonicalCogValidation(True, None, diagnostics, valid_mask_path=valid_mask_path, valid_mask_source=valid_mask_source)


def _mosaic_result_from_canonical(
    *,
    release: WaybackRelease,
    zoom: int,
    tile_range: tuple[int, int, int, int],
    bounds_3857: tuple[float, float, float, float],
    tile_count: int,
    canonical_cog_path: Path,
    valid_mask_path: Path,
    cache_dir: Path,
    reference_imagery_key: str,
    validation: CanonicalCogValidation,
) -> MosaicResult:
    return MosaicResult(
        identifier=release.identifier,
        release_date=str(release.release_date),
        zoom=zoom,
        tile_count=tile_count,
        available_tile_count=tile_count,
        missing_tile_count=0,
        tile_range=tile_range,
        bounds_3857=bounds_3857,
        png_path=canonical_cog_path,
        geotiff_path=canonical_cog_path,
        valid_mask_path=valid_mask_path,
        shared_cache_dir=cache_dir,
        cache_key=reference_imagery_key,
        materialized_in_request_dir=False,
        source_id=release.identifier,
        effective_date=str(release.release_date),
        metadata={
            "imagery_source_mode": "canonical_cog",
            "reference_imagery_key": reference_imagery_key,
            "canonical_cog_path": str(canonical_cog_path),
            "canonical_cog_validation": validation.diagnostics,
            "valid_mask_source": validation.valid_mask_source,
            "inference_reference_resolver_version": _INFERENCE_REFERENCE_RESOLVER_VERSION,
        },
    )


def _promote_mosaic_to_canonical(
    *,
    scene: MosaicResult,
    canonical_cog_path: Path,
    metadata_path: Path,
    reference_imagery_key: str,
    key_payload: dict[str, object],
    normalized_aoi: dict[str, Any],
) -> CanonicalCogValidation:
    ensure_reference_imagery_cog(
        scene.geotiff_path,
        canonical_cog_path,
        valid_mask_path=scene.valid_mask_path,
        aoi_geojson=normalized_aoi,
        project_id=None,
        release_identifier=scene.identifier,
    )
    existing_metadata = read_reference_imagery_cache_metadata(metadata_path)
    metadata = build_reference_imagery_cache_metadata(
        reference_imagery_key=reference_imagery_key,
        key_payload=key_payload,
        canonical_cog_path=canonical_cog_path,
        existing_metadata=existing_metadata,
    )
    metadata["source_wayback_mosaic_cache_key"] = scene.cache_key
    metadata["source_wayback_mosaic_dir"] = str(scene.shared_cache_dir)
    write_reference_imagery_cache_metadata(metadata_path, metadata)
    return validate_canonical_cog_for_inference(
        canonical_cog_path=canonical_cog_path,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_imagery_key,
        expected_key_payload=key_payload,
        normalized_aoi=normalized_aoi,
    )


def get_or_create_inference_reference_imagery(
    *,
    release: WaybackRelease,
    normalized_aoi: dict[str, Any],
    bbox: dict[str, float],
    settings: Settings,
    zoom: int,
    available_tiles: frozenset[tuple[int, int]] | None,
    source_role: str,
    out_dir: Path,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> MosaicResult:
    tile_range, bounds_3857, _width, _height, tile_count = _tile_grid_for_bbox(bbox, zoom)
    key_payload = _reference_key_payload(
        release=release,
        normalized_aoi=normalized_aoi,
        settings=settings,
        zoom=zoom,
        tile_range=tile_range,
        bounds_3857=bounds_3857,
    )
    reference_imagery_key = build_reference_imagery_key(key_payload)
    if settings.reference_imagery_cache_dir is None:
        fallback_reason = "reference_imagery_cache_dir_missing"
        canonical_cog_path = None
        metadata_path = None
        validation = CanonicalCogValidation(False, fallback_reason, {})
    else:
        canonical_cog_path = reference_imagery_cache_cog_path(settings.reference_imagery_cache_dir, reference_imagery_key)
        metadata_path = reference_imagery_cache_metadata_path(settings.reference_imagery_cache_dir, reference_imagery_key)
        validation = validate_canonical_cog_for_inference(
            canonical_cog_path=canonical_cog_path,
            metadata_path=metadata_path,
            expected_reference_imagery_key=reference_imagery_key,
            expected_key_payload=key_payload,
            normalized_aoi=normalized_aoi,
        )
        if validation.valid and validation.valid_mask_path is not None:
            LOGGER.info(
                "INFERENCE_REFERENCE_IMAGERY_CANONICAL_HIT releaseIdentifier=%s sourceRole=%s referenceImageryKey=%s canonicalCogPath=%s validMaskSource=%s",
                release.identifier,
                source_role,
                reference_imagery_key,
                canonical_cog_path,
                validation.valid_mask_source,
            )
            return _mosaic_result_from_canonical(
                release=release,
                zoom=zoom,
                tile_range=tile_range,
                bounds_3857=bounds_3857,
                tile_count=tile_count,
                canonical_cog_path=canonical_cog_path,
                valid_mask_path=validation.valid_mask_path,
                cache_dir=canonical_cog_path.parent,
                reference_imagery_key=reference_imagery_key,
                validation=validation,
            )

    LOGGER.info(
        "INFERENCE_REFERENCE_IMAGERY_FALLBACK releaseIdentifier=%s sourceRole=%s referenceImageryKey=%s reason=%s",
        release.identifier,
        source_role,
        reference_imagery_key,
        validation.reason,
    )
    scene = download_wayback_mosaic(
        release,
        bbox,
        settings=settings,
        zoom=zoom,
        out_dir=out_dir,
        label=source_role,
        max_tiles=None,
        available_tiles=available_tiles,
        progress_callback=progress_callback,
    )
    scene_metadata = dict(scene.metadata or {})
    scene_metadata.update(
        {
            "imagery_source_mode": "wayback_mosaic_fallback",
            "reference_imagery_key": reference_imagery_key,
            "canonical_cog_path": str(canonical_cog_path) if canonical_cog_path is not None else None,
            "canonical_cog_validation": validation.diagnostics,
            "fallback_reason": validation.reason,
            "valid_mask_source": "wayback_mosaic_valid_mask",
            "inference_reference_resolver_version": _INFERENCE_REFERENCE_RESOLVER_VERSION,
        }
    )
    if canonical_cog_path is not None and metadata_path is not None:
        try:
            promoted_validation = _promote_mosaic_to_canonical(
                scene=scene,
                canonical_cog_path=canonical_cog_path,
                metadata_path=metadata_path,
                reference_imagery_key=reference_imagery_key,
                key_payload=key_payload,
                normalized_aoi=normalized_aoi,
            )
            scene_metadata["canonical_cog_promoted"] = bool(promoted_validation.valid)
            scene_metadata["canonical_cog_validation_after_promotion"] = promoted_validation.diagnostics
        except Exception as exc:  # noqa: BLE001 - fallback inference remains valid; promotion is a cache optimization.
            scene_metadata["canonical_cog_promoted"] = False
            scene_metadata["canonical_cog_promotion_error"] = f"{type(exc).__name__}: {exc}"
            LOGGER.warning(
                "INFERENCE_REFERENCE_IMAGERY_PROMOTION_FAILED releaseIdentifier=%s sourceRole=%s referenceImageryKey=%s reason=%s",
                release.identifier,
                source_role,
                reference_imagery_key,
                scene_metadata["canonical_cog_promotion_error"],
            )
    return MosaicResult(**{**scene.__dict__, "metadata": scene_metadata})
