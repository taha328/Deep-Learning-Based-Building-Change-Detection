from __future__ import annotations

import math
from dataclasses import dataclass
import logging

from src.config import ModeLimits, Settings
from src.domain.tiling import (
    estimate_patch_count,
    intersecting_tiles_for_aoi,
    pixel_size_m_at_tile,
    scene_tile_count,
    tile_range_for_bbox,
)
from src.domain.wayback import WaybackRelease
from src.schemas import RunRequest, ValidationRequest, ValidationResponse
from src.utils.geometry import bounds_dict, geodesic_area_m2, normalized_aoi_geojson, parse_aoi_geometry
from src.utils.hashing import build_request_hash


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedRequest:
    normalized_aoi: dict
    area_m2: float
    tile_count_per_scene: int
    t1_release: WaybackRelease
    t2_release: WaybackRelease
    mode_limits: ModeLimits
    request_hash: str


def _find_release(releases: list[WaybackRelease], identifier: str) -> WaybackRelease:
    for release in releases:
        if release.identifier == identifier:
            return release
    raise ValueError(f"Unknown Wayback release: {identifier}")


def _validate_thresholds(request: ValidationRequest, settings: Settings) -> tuple[float, float]:
    change_threshold = request.change_threshold if request.change_threshold is not None else settings.change_threshold
    semantic_threshold = settings.semantic_threshold
    if not 0.0 <= change_threshold <= 1.0:
        raise ValueError("change_threshold must be between 0.0 and 1.0.")
    if not 0.0 <= semantic_threshold <= 1.0:
        raise ValueError("semantic_threshold must be between 0.0 and 1.0.")
    if request.old_building_mask_dilation_pixels is not None and request.old_building_mask_dilation_pixels < 0:
        raise ValueError("old_building_mask_dilation_pixels must be greater than or equal to 0.")
    if request.new_building_core_distance_pixels is not None and request.new_building_core_distance_pixels < 0:
        raise ValueError("new_building_core_distance_pixels must be greater than or equal to 0.")
    return change_threshold, semantic_threshold


def _pixel_area_estimate_m2(normalized_aoi: dict, settings: Settings) -> float:
    geometry = parse_aoi_geometry(normalized_aoi)
    x_min, _, y_min, _ = tile_range_for_bbox(bounds_dict(geometry), settings.wayback_preferred_inference_zoom)
    pixel_width_m, pixel_height_m = pixel_size_m_at_tile(x_min, y_min, settings.wayback_preferred_inference_zoom)
    return max(pixel_width_m * pixel_height_m, 0.01)


def _estimated_inference_patch_count(bbox: dict[str, float], settings: Settings) -> int:
    height, width = _estimated_model_input_size_px(bbox, settings)
    return estimate_patch_count(height, width, settings.patch_size, settings.stride)


def _estimated_model_input_size_px(bbox: dict[str, float], settings: Settings) -> tuple[int, int]:
    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, settings.wayback_preferred_inference_zoom)
    width = (x_max - x_min + 1) * 256
    height = (y_max - y_min + 1) * 256
    return height, width


def _requires_bandon_minimum_input_guard(request: ValidationRequest, settings: Settings) -> bool:
    backend = request.inference_backend or settings.inference_backend
    return backend == "bandon_mps"


def validate_request(
    request: ValidationRequest,
    *,
    releases: list[WaybackRelease],
    settings: Settings,
    remote_patch_budget_enabled: bool = False,
    request_hash_context: dict[str, object] | None = None,
) -> tuple[ValidationResponse, PreparedRequest | None]:
    warnings: list[str] = []
    blocking_errors: list[str] = []

    try:
        change_threshold, semantic_threshold = _validate_thresholds(request, settings)
        geometry = parse_aoi_geometry(request.aoi_geojson)
        normalized = normalized_aoi_geojson(request.aoi_geojson)
    except ValueError as exc:
        return (
            ValidationResponse(
                valid=False,
                normalized_aoi=None,
                estimated_tile_count_t1=0,
                estimated_tile_count_t2=0,
                estimated_total_tiles=0,
                estimated_area_m2=0.0,
                warnings=[],
                blocking_errors=[str(exc)],
                recommended_mode="fast_preview",
            ),
            None,
        )

    t1_release = _find_release(releases, request.t1_release)
    t2_release = _find_release(releases, request.t2_release)
    threshold_source = "request_override" if request.change_threshold is not None else "backend_settings_env"
    if request.semantic_threshold is not None:
        warnings.append(
            "Request semantic_threshold override was ignored because current local backends do not apply it to final outputs."
        )
    if t1_release.identifier == t2_release.identifier:
        blocking_errors.append("t1_release and t2_release must be different.")
    if t1_release.release_date >= t2_release.release_date:
        blocking_errors.append("t1_release must be chronologically earlier than t2_release.")

    area_m2 = geodesic_area_m2(geometry)
    bbox = bounds_dict(geometry)
    tile_count = scene_tile_count(bbox, settings.wayback_preferred_inference_zoom)
    patch_count = _estimated_inference_patch_count(bbox, settings)
    estimated_input_height, estimated_input_width = _estimated_model_input_size_px(bbox, settings)
    total_tiles = tile_count * 2

    preview_ok = area_m2 <= settings.preview_limits.max_area_m2 and tile_count <= settings.preview_limits.max_scene_tiles
    full_ok = area_m2 <= settings.full_limits.max_area_m2 and tile_count <= settings.full_limits.max_scene_tiles

    recommended_mode = "fast_preview" if preview_ok else "full_run"
    mode_limits = settings.get_mode_limits(request.mode)

    if area_m2 > mode_limits.max_area_m2:
        warnings.append(
            f"AOI area {area_m2:,.0f} m² exceeds the {mode_limits.label} limit of {mode_limits.max_area_m2:,.0f} m²."
            " The request remains allowed, but it may take significantly longer."
        )
    if tile_count > mode_limits.max_scene_tiles:
        warnings.append(
            f"AOI requires {tile_count} tiles per date, exceeding the {mode_limits.label} tile budget of {mode_limits.max_scene_tiles}."
            " The request remains allowed, but imagery download will be much heavier."
        )
    if remote_patch_budget_enabled and patch_count > mode_limits.max_inference_patches_per_scene:
        warnings.append(
            f"AOI requires {patch_count} inference patches per date, exceeding the {mode_limits.label} "
            f"guidance of {mode_limits.max_inference_patches_per_scene}. The request remains allowed, "
            "but local inference may take longer."
        )
    if _requires_bandon_minimum_input_guard(request, settings):
        min_input_px = settings.bandon_min_model_input_size_px
        if estimated_input_height < min_input_px or estimated_input_width < min_input_px:
            blocking_errors.append(
                "AOI is too small for BANDON inference at the selected zoom. "
                f"Estimated aligned model input is {estimated_input_width}x{estimated_input_height} pixels, "
                f"but BANDON requires at least {min_input_px}x{min_input_px} pixels based on the active "
                "test_cfg crop_size. Increase the AOI size or use a supported minimum smoke-test AOI."
            )
    heavy_batch = tile_count > settings.wayback_heavy_batch_tile_threshold
    if heavy_batch:
        warnings.append(
            f"AOI requires {tile_count} Wayback tiles per date at z={settings.wayback_preferred_inference_zoom}; "
            "this is classified as a heavy imagery download batch."
        )

    if request.mode == "fast_preview" and full_ok and not preview_ok:
        warnings.append("AOI fits Full Run but exceeds Fast Preview. Switch modes to continue.")
    elif request.mode == "fast_preview" and not preview_ok:
        warnings.append(
            "AOI exceeds the Fast Preview guidance. Full Run is recommended, but the request remains allowed."
        )
    if request.mode == "full_run" and not full_ok:
        warnings.append(
            "AOI exceeds the historical Full Run guidance. The request remains allowed, but runtime and disk usage may grow substantially."
        )

    response = ValidationResponse(
        valid=not blocking_errors,
        normalized_aoi=normalized,
        estimated_tile_count_t1=tile_count,
        estimated_tile_count_t2=tile_count,
        estimated_total_tiles=total_tiles,
        estimated_area_m2=round(area_m2, 2),
        warnings=warnings,
        blocking_errors=blocking_errors,
        recommended_mode=recommended_mode,
        details={
            "preferred_zoom": settings.wayback_preferred_inference_zoom,
            "min_zoom": settings.min_zoom,
            "tile_count_per_scene": tile_count,
            "inference_patch_count": patch_count,
            "estimated_model_input_width_px": estimated_input_width,
            "estimated_model_input_height_px": estimated_input_height,
            "bandon_min_model_input_size_px": settings.bandon_min_model_input_size_px
            if _requires_bandon_minimum_input_guard(request, settings)
            else None,
            "heavy_batch": heavy_batch,
            "heavy_batch_tile_threshold": settings.wayback_heavy_batch_tile_threshold,
            "recommended_tile_concurrency": settings.wayback_tile_max_concurrency,
            "change_threshold": change_threshold,
            "semantic_threshold": semantic_threshold,
            "threshold_source": threshold_source,
        },
    )

    if blocking_errors:
        return response, None

    hash_payload = {
        "pipeline": "building_change_pipeline_v4",
        "inference_backend": settings.inference_backend,
        "patch_size": settings.patch_size,
        "stride": settings.stride,
        "inference_tiled_mode_auto": settings.inference_tiled_mode_auto,
        "inference_tile_size": settings.inference_tile_size,
        "inference_tile_overlap": settings.inference_tile_overlap,
        "inference_max_in_memory_pixels": settings.inference_max_in_memory_pixels,
        "inference_heavy_batch_tile_threshold": settings.inference_heavy_batch_tile_threshold,
        "aoi_geojson": normalized,
        "t1_release": t1_release.identifier,
        "t2_release": t2_release.identifier,
        "mode": request.mode,
        "change_threshold": change_threshold,
        "threshold_source": threshold_source,
        "min_new_building_pixels": request.min_new_building_pixels,
        "min_new_building_area_m2": request.min_new_building_area_m2,
        "old_building_mask_dilation_pixels": request.old_building_mask_dilation_pixels
        if request.old_building_mask_dilation_pixels is not None
        else settings.default_old_building_mask_dilation_pixels,
        "new_building_core_distance_pixels": request.new_building_core_distance_pixels
        if request.new_building_core_distance_pixels is not None
        else settings.default_new_building_core_distance_pixels,
        "merge_close_gap_m": request.merge_close_gap_m or settings.default_merge_close_gap_m,
        "building_block_gap_m": request.building_block_gap_m or settings.default_building_block_gap_m,
        "buffer_distances_m": request.buffer_distances_m or list(settings.default_buffer_distances_m),
        "keep_disjoint_buffer_parts_separate": request.keep_disjoint_buffer_parts_separate,
        "road_constraint_layer_path": request.road_constraint_layer_path,
        "addition_min_area_m2": settings.addition_min_area_m2,
        "addition_max_existing_overlap_ratio": settings.addition_max_existing_overlap_ratio,
        "addition_thin_artifact_max_area_m2": settings.addition_thin_artifact_max_area_m2,
        "addition_thinness_min_ratio": settings.addition_thinness_min_ratio,
        "addition_edge_buffer_m": settings.addition_edge_buffer_m,
        "addition_max_edge_overlap_ratio": settings.addition_max_edge_overlap_ratio,
        "addition_thin_artifact_max_mean_probability": settings.addition_thin_artifact_max_mean_probability,
    }
    if request_hash_context:
        hash_payload.update(request_hash_context)

    prepared = PreparedRequest(
        normalized_aoi=normalized,
        area_m2=area_m2,
        tile_count_per_scene=tile_count,
        t1_release=t1_release,
        t2_release=t2_release,
        mode_limits=mode_limits,
        request_hash=build_request_hash(hash_payload),
    )
    return response, prepared


def resolve_min_new_building_pixels(
    request: RunRequest,
    *,
    normalized_aoi: dict,
    settings: Settings,
) -> int:
    if request.min_new_building_pixels is not None:
        return max(int(request.min_new_building_pixels), 1)
    if request.min_new_building_area_m2 is not None:
        pixel_area_m2 = _pixel_area_estimate_m2(normalized_aoi, settings)
        return max(int(math.ceil(float(request.min_new_building_area_m2) / pixel_area_m2)), 1)
    return settings.default_min_new_building_pixels
