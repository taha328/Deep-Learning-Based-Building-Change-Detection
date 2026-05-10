from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from src.api.deps import get_app_settings
from src.api.errors import raise_api_error
from src.core_api import (
    get_temporal_project_api,
    import_temporal_override_api,
    list_temporal_projects_api,
    run_temporal_project_api,
    save_temporal_project_api,
    validate_temporal_project_api,
)
from src.services.temporal_projects import create_temporal_project_bundle, temporal_project_response_payload
from src.services.temporal_reference_imagery import (
    build_reference_tilejson_payload_cached,
    reference_imagery_version_token,
    render_reference_tile_png_cached,
    resolve_temporal_reference_cog_cached,
)
from src.services.reference_layers import (
    ReferenceLayerError,
    delete_reference_layer,
    get_reference_layer,
    import_reference_layer,
    list_reference_layers,
    preflight_reference_layer,
    update_reference_layer,
)
from src.schemas import (
    ReferenceLayer,
    ReferenceLayerPatchRequest,
    ReferenceLayerPreflightResponse,
    TemporalOverrideRequest,
    TemporalProject,
    TemporalProjectRunResponse,
    TemporalProjectSaveRequest,
    TemporalProjectSaveResponse,
    TemporalProjectValidationResponse,
    TemporalReferenceImagery,
)


router = APIRouter()
logger = logging.getLogger(__name__)


class TemporalProjectOverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_geojson: dict[str, Any]


def _resolve_milestone_reference_imagery(
    project_id: str,
    release_identifier: str,
    *,
    settings,
):
    base_project_dir = settings.temporal_projects_dir.resolve()
    direct_project_dir = (base_project_dir / project_id).resolve()
    direct_cog_path = direct_project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
    try:
        direct_project_dir.relative_to(base_project_dir)
        direct_cog_path.relative_to(base_project_dir)
    except ValueError:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "invalid_project_id", "Invalid temporal project identifier.")
    if direct_cog_path.is_file():
        direct_reference = TemporalReferenceImagery(
            image_png_data_url=None,
            raster_bounds_wgs84=None,
            storage_strategy="raster_tiles",
            cog_path=str(direct_cog_path),
            tilejson_url=f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/tilejson.json",
            tiles_url_template=f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/tiles/{{z}}/{{x}}/{{y}}.png",
            tile_size=256,
        )
        cog_info = resolve_temporal_reference_cog_cached(
            project_id=project_id,
            release_identifier=release_identifier,
            reference_imagery=direct_reference,
        )
        if cog_info is not None:
            return release_identifier, cog_info

    project = get_temporal_project_api(project_id, settings=settings)
    milestone = next((item for item in project.milestones if item.release_identifier == release_identifier), None)
    if milestone is None:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", f"Unknown milestone: {release_identifier}")
    reference_imagery = milestone.reference_imagery
    if reference_imagery is None or not reference_imagery.cog_path:
        project_dir = Path(project.project_dir).expanduser().resolve() if project.project_dir else (settings.temporal_projects_dir / project_id)
        selected_cog_path = project_dir / "milestones" / release_identifier / "reference_imagery_cog.tif"
        if selected_cog_path.is_file():
            reference_imagery = TemporalReferenceImagery(
                image_path=reference_imagery.image_path if reference_imagery else None,
                image_png_data_url=None,
                raster_bounds_wgs84=reference_imagery.raster_bounds_wgs84 if reference_imagery else None,
                storage_strategy="raster_tiles",
                cog_path=str(selected_cog_path),
                tilejson_url=f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/tilejson.json",
                tiles_url_template=f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/reference/tiles/{{z}}/{{x}}/{{y}}.png",
                minzoom=reference_imagery.minzoom if reference_imagery else None,
                maxzoom=reference_imagery.maxzoom if reference_imagery else None,
                tile_size=reference_imagery.tile_size if reference_imagery and reference_imagery.tile_size else 256,
            )
            logger.info(
                "SELECTED_REFERENCE_IMAGERY_METADATA_REPAIRED projectId=%s releaseIdentifier=%s cogPath=%s",
                project_id,
                release_identifier,
                selected_cog_path,
            )
    cog_info = resolve_temporal_reference_cog_cached(
        project_id=project_id,
        release_identifier=release_identifier,
        reference_imagery=reference_imagery,
    )
    if cog_info is None:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", "Reference imagery COG not found for milestone.")
    return milestone.release_identifier, cog_info


@router.get("")
def list_projects(
    include_cached_runs: bool = False,
    settings=Depends(get_app_settings),
) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in list_temporal_projects_api(settings=settings, include_cached_runs=include_cached_runs)]


@router.get("/{project_id}")
def get_project(project_id: str, settings=Depends(get_app_settings)) -> dict[str, object]:
    started_at = time.perf_counter()
    try:
        project = get_temporal_project_api(project_id, settings=settings)
        payload = temporal_project_response_payload(project, settings)
        logger.info(
            "TEMPORAL_PROJECT_LOADED projectId=%s milestoneCount=%s durationMs=%s",
            project_id,
            len(payload.get("milestones", [])) if isinstance(payload, dict) else None,
            round((time.perf_counter() - started_at) * 1000, 2),
        )
        return payload
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    finally:
        logger.info("PROJECT_LOAD_SKIPPED_COG_HYDRATION projectId=%s value=true", project_id)


@router.post("")
def save_project(body: TemporalProjectSaveRequest, settings=Depends(get_app_settings)) -> TemporalProjectSaveResponse:
    saved_project = save_temporal_project_api(body.project, settings=settings)
    return TemporalProjectSaveResponse(
        project_id=saved_project.project_id,
        updated_at=saved_project.updated_at,
        download_bundle_path=saved_project.download_bundle_path,
    )


@router.post("/validate")
def validate_project(
    body: TemporalProjectSaveRequest,
    settings=Depends(get_app_settings),
) -> TemporalProjectValidationResponse:
    return validate_temporal_project_api(body.project, settings=settings)


@router.post("/{project_id}/run")
def run_project(
    project_id: str,
    request: Request,
    settings=Depends(get_app_settings),
) -> TemporalProjectRunResponse:
    try:
        return run_temporal_project_api(project_id, settings=settings, x_ip_token=request.headers.get("x-ip-token"))
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except Exception as exc:  # noqa: BLE001
        return TemporalProjectRunResponse(
            success=False,
            error_message=f"{type(exc).__name__}: {exc}",
            project=TemporalProject(
                project_id=project_id,
                name="Invalid request",
                created_at="1970-01-01T00:00:00Z",
                updated_at="1970-01-01T00:00:00Z",
            ),
        )


@router.post("/{project_id}/milestones/{release_identifier}/override")
def import_override(
    project_id: str,
    release_identifier: str,
    body: TemporalProjectOverrideBody,
    settings=Depends(get_app_settings),
) -> TemporalProjectRunResponse:
    request = TemporalOverrideRequest(
        project_id=project_id,
        release_identifier=release_identifier,
        override_geojson=body.override_geojson,
    )
    return import_temporal_override_api(request, settings=settings)


@router.get("/{project_id}/milestones/{release_identifier}/reference/tilejson.json")
def get_reference_tilejson(
    project_id: str,
    release_identifier: str,
    request: Request,
    settings=Depends(get_app_settings),
) -> JSONResponse:
    started_at = time.perf_counter()
    release_name, cog_info = _resolve_milestone_reference_imagery(project_id, release_identifier, settings=settings)
    tile_url = request.url_for(
        "get_reference_tile",
        project_id=project_id,
        release_identifier=release_identifier,
        z="0",
        x="0",
        y="0",
    )
    tiles_url = (
        str(tile_url).replace("/0/0/0.png", "/{z}/{x}/{y}.png")
        + f"?v={reference_imagery_version_token(cog_info)}"
    )
    payload, cache_hit = build_reference_tilejson_payload_cached(
        project_id=project_id,
        release_identifier=release_identifier,
        cog_info=cog_info,
        name=f"{project_id}:{release_name}",
        tiles_url=tiles_url,
    )
    tilejson_cache_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "TILEJSON_CACHE_%s projectId=%s releaseIdentifier=%s cacheKey=%s durationMs=%s",
        "HIT" if cache_hit else "MISS",
        project_id,
        release_identifier,
        f"{project_id}:{release_identifier}:{cog_info.cog_path}:{cog_info.cog_path.stat().st_mtime_ns}:{cog_info.cog_path.stat().st_size}",
        tilejson_cache_ms,
    )
    logger.info("TILEJSON_SELECTED_RELEASE_ONLY projectId=%s releaseIdentifier=%s value=true", project_id, release_identifier)
    response = JSONResponse(
        payload,
        headers={"Cache-Control": "public, max-age=0, must-revalidate"},
    )
    logger.info(
        "TILEJSON_SERVED projectId=%s releaseIdentifier=%s storageStrategy=%s cogExists=%s durationMs=%s",
        project_id,
        release_identifier,
        "raster_tiles",
        cog_info.cog_path.is_file(),
        round((time.perf_counter() - started_at) * 1000, 2),
    )
    return response


@router.get("/{project_id}/milestones/{release_identifier}/reference/tiles/{z}/{x}/{y}.png")
def get_reference_tile(
    project_id: str,
    release_identifier: str,
    z: int,
    x: int,
    y: int,
    settings=Depends(get_app_settings),
) -> Response:
    started_at = time.perf_counter()
    if z < 0 or x < 0 or y < 0:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "invalid_tile", "Tile coordinates must be non-negative.")
    metadata_started_at = time.perf_counter()
    _, cog_info = _resolve_milestone_reference_imagery(project_id, release_identifier, settings=settings)
    metadata_ms = round((time.perf_counter() - metadata_started_at) * 1000, 2)
    result = render_reference_tile_png_cached(
        project_id=project_id,
        release_identifier=release_identifier,
        cog_info=cog_info,
        z=z,
        x=x,
        y=y,
    )
    response = Response(
        content=result.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
    logger.info(
        "TILE_METADATA_LOOKUP_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        metadata_ms,
    )
    logger.info(
        "TILE_COG_OPEN_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.timings_ms.get("cog_open", 0.0),
    )
    logger.info(
        "TILE_WINDOW_CALC_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.timings_ms.get("window_calc", 0.0),
    )
    logger.info(
        "TILE_READ_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.timings_ms.get("read", 0.0),
    )
    logger.info(
        "TILE_REPROJECT_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.timings_ms.get("reproject", 0.0),
    )
    logger.info(
        "TILE_ENCODE_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.timings_ms.get("encode", 0.0),
    )
    logger.info(
        "TILE_TOTAL_MS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s value=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        round((time.perf_counter() - started_at) * 1000, 2),
    )
    logger.info(
        "TILE_CACHE_%s projectId=%s releaseIdentifier=%s z=%s x=%s y=%s",
        "HIT" if result.cache_hit else "MISS",
        project_id,
        release_identifier,
        z,
        x,
        y,
    )
    logger.info(
        (
            "TILE_SERVED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cacheHeaders=%s "
            "cogPath=%s cogCrs=%s cogTransform=%s cogBounds=%s cogWidth=%s cogHeight=%s hasOverviews=%s "
            "datasetBandCount=%s datasetHasAlphaBand=%s datasetHasInternalMask=%s tileHasMask=%s "
            "outputPngHasAlpha=%s transparentPixelCount=%s opaquePixelCount=%s warningCount=%s"
        ),
        project_id,
        release_identifier,
        z,
        x,
        y,
        round((time.perf_counter() - started_at) * 1000, 2),
        response.headers.get("Cache-Control"),
        cog_info.cog_path,
        cog_info.cog_crs,
        cog_info.cog_transform,
        cog_info.cog_bounds,
        cog_info.cog_width,
        cog_info.cog_height,
        cog_info.has_overviews,
        result.dataset_band_count,
        result.dataset_has_alpha_band,
        result.dataset_has_internal_mask,
        result.tile_has_mask,
        result.output_png_has_alpha,
        result.transparent_pixel_count,
        result.opaque_pixel_count,
        result.warning_count,
    )
    return response


@router.get("/{project_id}/reference-layers")
def list_project_reference_layers(project_id: str, settings=Depends(get_app_settings)) -> list[ReferenceLayer]:
    started_at = time.perf_counter()
    try:
        layers = list_reference_layers(project_id, settings)
        logger.info(
            "REFERENCE_LAYERS_LISTED projectId=%s count=%s durationMs=%s",
            project_id,
            len(layers),
            round((time.perf_counter() - started_at) * 1000, 2),
        )
        return layers
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ReferenceLayerError as exc:
        raise_api_error(exc.status_code, exc.code, exc.message, exc.details)


@router.post("/{project_id}/reference-layers/preflight")
async def preflight_project_reference_layer(
    project_id: str,
    file: UploadFile = File(...),
    scope: str = Form("aoi_clipped"),
    settings=Depends(get_app_settings),
) -> ReferenceLayerPreflightResponse:
    try:
        return await preflight_reference_layer(project_id, file, settings=settings, scope=scope)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ReferenceLayerError as exc:
        raise_api_error(exc.status_code, exc.code, exc.message, exc.details)


@router.post("/{project_id}/reference-layers")
async def import_project_reference_layer(
    project_id: str,
    file: UploadFile = File(...),
    name: str = Form(...),
    scope: str = Form("aoi_clipped"),
    rendering_strategy: str = Form("auto"),
    settings=Depends(get_app_settings),
) -> ReferenceLayer:
    try:
        return await import_reference_layer(
            project_id,
            file,
            settings=settings,
            name=name,
            scope=scope,
            rendering_strategy=rendering_strategy,
        )
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ReferenceLayerError as exc:
        raise_api_error(exc.status_code, exc.code, exc.message, exc.details)


@router.get("/{project_id}/reference-layers/{layer_id}")
def get_project_reference_layer(project_id: str, layer_id: str, settings=Depends(get_app_settings)) -> ReferenceLayer:
    try:
        return get_reference_layer(project_id, layer_id, settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ReferenceLayerError as exc:
        raise_api_error(exc.status_code, exc.code, exc.message, exc.details)


@router.patch("/{project_id}/reference-layers/{layer_id}")
def patch_project_reference_layer(
    project_id: str,
    layer_id: str,
    body: ReferenceLayerPatchRequest,
    settings=Depends(get_app_settings),
) -> ReferenceLayer:
    try:
        return update_reference_layer(project_id, layer_id, body, settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ReferenceLayerError as exc:
        raise_api_error(exc.status_code, exc.code, exc.message, exc.details)


@router.delete("/{project_id}/reference-layers/{layer_id}")
def delete_project_reference_layer(project_id: str, layer_id: str, settings=Depends(get_app_settings)) -> dict[str, bool]:
    try:
        delete_reference_layer(project_id, layer_id, settings)
        return {"deleted": True}
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ReferenceLayerError as exc:
        raise_api_error(exc.status_code, exc.code, exc.message, exc.details)


@router.post("/{project_id}/export-bundle")
def export_bundle(project_id: str, settings=Depends(get_app_settings)) -> dict[str, str]:
    try:
        bundle_path = create_temporal_project_bundle(project_id, settings=settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    return {
        "path": str(bundle_path),
        "filename": bundle_path.name,
        "label": "Export QGIS",
    }
