from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi import File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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
from src.services.temporal_projects import (
    build_temporal_artifact_vector_tilejson,
    create_temporal_project_bundle,
    load_temporal_project_compact_payload,
    load_temporal_project_response_payload,
    render_temporal_artifact_vector_tile,
    resolve_temporal_project_artifact_path,
)
from src.services.temporal_exports import (
    TEMPORAL_RESULTS_EXPORT_FILENAMES,
    TEMPORAL_RESULTS_EXPORT_MEDIA_TYPES,
    build_temporal_results_export_file,
    resolve_cached_temporal_results_export_file,
)
from src.services.temporal_reference_imagery import (
    build_reference_tilejson_payload_cached,
    ensure_reference_tile_png_cached_on_disk,
    reference_imagery_version_token,
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
    TemporalProjectRunRequest,
    TemporalProjectRunResponse,
    TemporalProjectSaveRequest,
    TemporalProjectSaveResponse,
    TemporalProjectValidationResponse,
    TemporalReferenceImagery,
    TemporalResultsExportRequest,
)


router = APIRouter()
logger = logging.getLogger(__name__)


_EXPORT_EXPOSE_HEADERS = "Content-Disposition, Content-Length, Content-Type"
_EXPORT_JOB_DOWNLOAD_TOKEN_TTL = timedelta(hours=6)
_EXPORT_JOB_LARGE_THRESHOLD_BYTES = 100 * 1024 * 1024
_EXPORT_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="temporal-export")
_EXPORT_JOB_LOCK = threading.Lock()
_EXPORT_JOB_SECRET = secrets.token_bytes(32)


@dataclass
class TemporalResultsExportJobState:
    job_id: str
    project_id: str
    format: str
    perimeter: dict[str, Any]
    include_rasters: bool
    include_offline_package: bool
    status: str
    created_at: datetime
    updated_at: datetime
    progress: float | None = None
    path: Path | None = None
    file_size_bytes: int | None = None
    filename: str | None = None
    content_type: str | None = None
    download_url: str | None = None
    generated_at: datetime | None = None
    expires_at: datetime | None = None
    error_message: str | None = None
    cache_key: str | None = None
    estimated_large_export: bool = False


_EXPORT_JOBS: dict[str, TemporalResultsExportJobState] = {}


def _results_export_filename(project_id: str, normalized_format: str) -> str:
    filename = TEMPORAL_RESULTS_EXPORT_FILENAMES[normalized_format]
    if normalized_format == "shapefile":
        return f"resultats_{project_id}_{filename}"
    return f"resultats_{project_id}.{normalized_format}"


def _results_export_file_response(project_id: str, normalized_format: str, path: Path) -> FileResponse:
    if path.name.endswith(".partial"):
        logger.info(
            "EXPORT_CACHE_FILE_INVALID projectId=%s format=%s path=%s reason=partial_file_response_blocked",
            project_id,
            normalized_format,
            path,
        )
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "export_file_invalid", "Export file is incomplete.")
    try:
        stat_result = path.stat()
    except OSError as exc:
        logger.info(
            "EXPORT_CACHE_FILE_INVALID projectId=%s format=%s path=%s reason=missing_or_unreadable_file",
            project_id,
            normalized_format,
            path,
        )
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "export_file_invalid", f"Export file is not readable: {exc}")
    if stat_result.st_size <= 0:
        logger.info(
            "EXPORT_CACHE_FILE_INVALID projectId=%s format=%s path=%s reason=empty_file",
            project_id,
            normalized_format,
            path,
        )
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "export_file_invalid", "Export file is empty.")

    headers = {
        "Access-Control-Expose-Headers": _EXPORT_EXPOSE_HEADERS,
        "Content-Length": str(stat_result.st_size),
        "X-Content-Type-Options": "nosniff",
    }
    filename = _results_export_filename(project_id, normalized_format)
    logger.info(
        "EXPORT_FILE_RESPONSE_READY projectId=%s format=%s path=%s bytes=%s filename=%s",
        project_id,
        normalized_format,
        path,
        stat_result.st_size,
        filename,
    )
    response = FileResponse(
        path,
        media_type=TEMPORAL_RESULTS_EXPORT_MEDIA_TYPES[normalized_format],
        filename=filename,
        headers=headers,
    )
    logger.info(
        "EXPORT_FILE_RESPONSE_HEADERS projectId=%s format=%s contentType=%s contentDisposition=%s contentLength=%s exposedHeaders=%s",
        project_id,
        normalized_format,
        response.headers.get("content-type"),
        response.headers.get("content-disposition"),
        response.headers.get("content-length"),
        response.headers.get("access-control-expose-headers"),
    )
    logger.info(
        "EXPORT_FILE_RESPONSE_SENT projectId=%s format=%s path=%s bytes=%s",
        project_id,
        normalized_format,
        path,
        stat_result.st_size,
    )
    return response


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _normalize_export_format_or_raise(export_format: str) -> str:
    normalized_format = export_format.lower()
    if normalized_format == "zip":
        normalized_format = "shapefile"
    if normalized_format not in TEMPORAL_RESULTS_EXPORT_FILENAMES:
        raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_export_format",
            f"Unsupported temporal results export format: {export_format}",
        )
    return normalized_format


def _json_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _results_export_metadata(path: Path) -> dict[str, Any]:
    metadata_path = path.with_name(f"{path.name}.metadata.json")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _path_digest(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _base64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _sign_export_download_token(payload: dict[str, Any]) -> str:
    encoded_payload = _base64url_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_EXPORT_JOB_SECRET, encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded_payload}.{_base64url_encode(signature)}"


def _decode_export_download_token(token: str) -> dict[str, Any]:
    try:
        encoded_payload, encoded_signature = token.split(".", maxsplit=1)
        expected = hmac.new(_EXPORT_JOB_SECRET, encoded_payload.encode("ascii"), hashlib.sha256).digest()
        supplied = _base64url_decode(encoded_signature)
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid signature")
        payload = json.loads(_base64url_decode(encoded_payload).decode("utf-8"))
    except Exception as exc:
        raise_api_error(status.HTTP_403_FORBIDDEN, "invalid_export_token", f"Invalid export download token: {exc}")
    if not isinstance(payload, dict):
        raise_api_error(status.HTTP_403_FORBIDDEN, "invalid_export_token", "Invalid export download token.")
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at < int(_utc_now().timestamp()):
        raise_api_error(status.HTTP_403_FORBIDDEN, "expired_export_token", "Export download token has expired.")
    return payload


def _export_job_response(job: TemporalResultsExportJobState) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "status": job.status,
        "progress": job.progress,
        "format": job.format,
        "scope": job.perimeter.get("mode", "project_aoi"),
        "perimeter": job.perimeter,
        "include_rasters": job.include_rasters,
        "include_offline_package": job.include_offline_package,
        "file_size_bytes": job.file_size_bytes,
        "filename": job.filename,
        "content_type": job.content_type,
        "download_url": job.download_url,
        "generated_at": _json_timestamp(job.generated_at),
        "expires_at": _json_timestamp(job.expires_at),
        "created_at": _json_timestamp(job.created_at),
        "updated_at": _json_timestamp(job.updated_at),
        "cache_key": job.cache_key,
        "estimated_large_export": job.estimated_large_export,
        "error_message": job.error_message,
    }


def _make_export_download_url(job: TemporalResultsExportJobState, path: Path) -> str:
    expires_at = _utc_now() + _EXPORT_JOB_DOWNLOAD_TOKEN_TTL
    payload = {
        "project_id": job.project_id,
        "job_id": job.job_id,
        "format": job.format,
        "cache_key": job.cache_key,
        "path_sha256": _path_digest(path),
        "exp": int(expires_at.timestamp()),
    }
    token = _sign_export_download_token(payload)
    job.expires_at = expires_at
    return (
        f"/api/temporal-projects/{quote(job.project_id)}/exports/jobs/{quote(job.job_id)}/download"
        f"?token={quote(token)}"
    )


def _mark_export_job_ready(job: TemporalResultsExportJobState, path: Path) -> None:
    stat_result = path.stat()
    metadata = _results_export_metadata(path)
    job.status = "succeeded"
    job.progress = 1.0
    job.path = path
    job.file_size_bytes = stat_result.st_size
    job.filename = _results_export_filename(job.project_id, job.format)
    job.content_type = TEMPORAL_RESULTS_EXPORT_MEDIA_TYPES[job.format]
    job.cache_key = str(metadata.get("cache_key") or "")
    job.estimated_large_export = stat_result.st_size >= _EXPORT_JOB_LARGE_THRESHOLD_BYTES or job.format == "shapefile"
    job.generated_at = _utc_now()
    job.updated_at = job.generated_at
    job.download_url = _make_export_download_url(job, path)
    logger.info(
        "EXPORT_JOB_READY projectId=%s jobId=%s format=%s path=%s bytes=%s cacheKey=%s",
        job.project_id,
        job.job_id,
        job.format,
        path,
        stat_result.st_size,
        job.cache_key,
    )


def _get_export_job_or_404(project_id: str, job_id: str) -> TemporalResultsExportJobState:
    with _EXPORT_JOB_LOCK:
        job = _EXPORT_JOBS.get(job_id)
    if job is None or job.project_id != project_id:
        raise_api_error(status.HTTP_404_NOT_FOUND, "export_job_not_found", "Export job was not found.")
    return job


def _run_export_job(job_id: str, settings) -> None:
    with _EXPORT_JOB_LOCK:
        job = _EXPORT_JOBS.get(job_id)
        if job is None:
            return
        job.status = "running"
        job.progress = 0.1
        job.updated_at = _utc_now()
        logger.info(
            "EXPORT_JOB_STATUS projectId=%s jobId=%s status=%s format=%s",
            job.project_id,
            job.job_id,
            job.status,
            job.format,
        )
    try:
        path = build_temporal_results_export_file(
            job.project_id,
            job.format,
            settings=settings,
            perimeter=job.perimeter,
            include_rasters=job.include_rasters,
            include_offline_package=job.include_offline_package,
        )
        with _EXPORT_JOB_LOCK:
            job = _EXPORT_JOBS.get(job_id)
            if job is not None:
                _mark_export_job_ready(job, path)
    except Exception as exc:  # noqa: BLE001
        with _EXPORT_JOB_LOCK:
            job = _EXPORT_JOBS.get(job_id)
            if job is not None:
                job.status = "failed"
                job.progress = 1.0
                job.error_message = f"{type(exc).__name__}: {exc}"
                job.updated_at = _utc_now()
                logger.exception(
                    "EXPORT_JOB_FAILED projectId=%s jobId=%s format=%s error=%s",
                    job.project_id,
                    job.job_id,
                    job.format,
                    exc,
                )


class TemporalProjectOverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_geojson: dict[str, Any]


class ReferenceTileCoordinate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    z: int = Field(ge=0, le=30)
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ReferenceTilePrewarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiles: list[ReferenceTileCoordinate] = Field(min_length=1)


def _validate_xyz_bounds(z: int, x: int, y: int) -> None:
    if z < 0 or x < 0 or y < 0:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "invalid_tile", "Tile coordinates must be non-negative.")
    max_coord = (2**z) - 1
    if x > max_coord or y > max_coord:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "invalid_tile", "Tile coordinates exceed the selected zoom bounds.")


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
        payload = load_temporal_project_response_payload(project_id, settings)
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


@router.get("/{project_id}/compact")
def get_project_compact(project_id: str, settings=Depends(get_app_settings)) -> dict[str, object]:
    try:
        return load_temporal_project_compact_payload(project_id, settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ValueError as exc:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "invalid_project_id", str(exc))


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
    body: TemporalProjectRunRequest | None = None,
    settings=Depends(get_app_settings),
) -> TemporalProjectRunResponse:
    try:
        return run_temporal_project_api(
            project_id,
            settings=settings,
            x_ip_token=request.headers.get("x-ip-token"),
            run_request=body,
        )
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


def _export_project_results(project_id: str, export_format: str, settings) -> FileResponse:
    normalized_format = export_format.lower()
    if normalized_format == "zip":
        normalized_format = "shapefile"
    if normalized_format not in TEMPORAL_RESULTS_EXPORT_FILENAMES:
        raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_export_format",
            f"Unsupported temporal results export format: {export_format}",
        )
    try:
        path = build_temporal_results_export_file(project_id, normalized_format, settings=settings)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ValueError as exc:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "unsupported_export_format", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "export_failed", f"{type(exc).__name__}: {exc}")
    return _results_export_file_response(project_id, normalized_format, path)


@router.head("/{project_id}/exports/results.{export_format}")
@router.get("/{project_id}/exports/results.{export_format}")
def export_project_results(
    project_id: str,
    export_format: str,
    mode: str | None = Query(default=None),
    settings=Depends(get_app_settings),
) -> FileResponse:
    if export_format.lower() == "topojson" and mode is not None:
        raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_export_mode",
            "TopoJSON export supports only the default clean mode.",
        )
    return _export_project_results(project_id, export_format, settings)


@router.head("/{project_id}/exports/results_shapefile.zip")
@router.get("/{project_id}/exports/results_shapefile.zip")
def export_project_results_shapefile(project_id: str, settings=Depends(get_app_settings)) -> FileResponse:
    return _export_project_results(project_id, "shapefile", settings)


@router.post("/{project_id}/exports/results")
def export_project_results_for_perimeter(
    project_id: str,
    body: TemporalResultsExportRequest,
    settings=Depends(get_app_settings),
) -> FileResponse:
    try:
        perimeter = body.perimeter.model_dump(mode="json")
        path = build_temporal_results_export_file(
            project_id,
            body.format,
            settings=settings,
            perimeter=perimeter,
            include_rasters=body.include_rasters,
            include_offline_package=body.include_offline_package,
        )
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except ValueError as exc:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "invalid_export_perimeter", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "export_failed", f"{type(exc).__name__}: {exc}")
    return _results_export_file_response(project_id, body.format, path)


@router.post("/{project_id}/exports/jobs")
def create_temporal_results_export_job(
    project_id: str,
    body: TemporalResultsExportRequest,
    settings=Depends(get_app_settings),
) -> dict[str, Any]:
    normalized_format = _normalize_export_format_or_raise(body.format)
    perimeter = body.perimeter.model_dump(mode="json")
    now = _utc_now()
    job = TemporalResultsExportJobState(
        job_id=uuid.uuid4().hex,
        project_id=project_id,
        format=normalized_format,
        perimeter=perimeter,
        include_rasters=body.include_rasters,
        include_offline_package=body.include_offline_package,
        status="queued",
        progress=0.0,
        created_at=now,
        updated_at=now,
    )
    logger.info(
        "EXPORT_JOB_CREATED projectId=%s jobId=%s format=%s scope=%s includeRasters=%s includeOfflinePackage=%s",
        project_id,
        job.job_id,
        normalized_format,
        perimeter.get("mode", "project_aoi"),
        body.include_rasters,
        body.include_offline_package,
    )
    try:
        cached_path = resolve_cached_temporal_results_export_file(
            project_id,
            normalized_format,
            settings=settings,
            perimeter=perimeter,
            include_rasters=body.include_rasters,
            include_offline_package=body.include_offline_package,
        )
    except ValueError as exc:
        raise_api_error(status.HTTP_400_BAD_REQUEST, "unsupported_export_format", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "EXPORT_JOB_CACHE_CHECK_FAILED projectId=%s jobId=%s format=%s error=%s",
            project_id,
            job.job_id,
            normalized_format,
            exc,
        )
        cached_path = None

    with _EXPORT_JOB_LOCK:
        _EXPORT_JOBS[job.job_id] = job
        if cached_path is not None:
            _mark_export_job_ready(job, cached_path)
            response_payload = _export_job_response(job)
        else:
            response_payload = _export_job_response(job)
            _EXPORT_JOB_EXECUTOR.submit(_run_export_job, job.job_id, settings)
    logger.info(
        "EXPORT_JOB_STATUS projectId=%s jobId=%s status=%s format=%s",
        project_id,
        job.job_id,
        job.status,
        normalized_format,
    )
    return response_payload


@router.get("/{project_id}/exports/jobs/{job_id}")
def get_temporal_results_export_job(project_id: str, job_id: str) -> dict[str, Any]:
    job = _get_export_job_or_404(project_id, job_id)
    logger.info(
        "EXPORT_JOB_STATUS projectId=%s jobId=%s status=%s format=%s bytes=%s",
        project_id,
        job_id,
        job.status,
        job.format,
        job.file_size_bytes,
    )
    return _export_job_response(job)


@router.get("/{project_id}/exports/jobs/{job_id}/download")
def download_temporal_results_export_job(project_id: str, job_id: str, token: str = Query(min_length=1)) -> FileResponse:
    job = _get_export_job_or_404(project_id, job_id)
    if job.status != "succeeded" or job.path is None:
        raise_api_error(status.HTTP_409_CONFLICT, "export_job_not_ready", "Export job is not ready for download.")
    payload = _decode_export_download_token(token)
    if (
        payload.get("project_id") != project_id
        or payload.get("job_id") != job_id
        or payload.get("format") != job.format
        or payload.get("cache_key") != job.cache_key
        or payload.get("path_sha256") != _path_digest(job.path)
    ):
        raise_api_error(status.HTTP_403_FORBIDDEN, "invalid_export_token", "Export download token does not match this job.")
    logger.info(
        "EXPORT_DIRECT_DOWNLOAD_READY projectId=%s jobId=%s format=%s path=%s bytes=%s",
        project_id,
        job_id,
        job.format,
        job.path,
        job.file_size_bytes,
    )
    response = _results_export_file_response(project_id, job.format, job.path)
    logger.info(
        "EXPORT_DIRECT_DOWNLOAD_SENT projectId=%s jobId=%s format=%s path=%s bytes=%s",
        project_id,
        job_id,
        job.format,
        job.path,
        job.file_size_bytes,
    )
    return response


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
    tile_version = reference_imagery_version_token(cog_info)
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
        headers={
            "Cache-Control": "public, max-age=300, stale-while-revalidate=86400",
            "ETag": f'"{tile_version}"',
        },
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


@router.get("/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}")
def get_milestone_artifact(
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    settings=Depends(get_app_settings),
) -> FileResponse:
    try:
        path, media_type = resolve_temporal_project_artifact_path(
            project_id=project_id,
            release_identifier=release_identifier,
            artifact_key=artifact_key,
            settings=settings,
        )
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}/tilejson.json")
def get_milestone_artifact_tilejson(
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    request: Request,
    settings=Depends(get_app_settings),
) -> JSONResponse:
    try:
        payload = build_temporal_artifact_vector_tilejson(
            project_id=project_id,
            release_identifier=release_identifier,
            artifact_key=artifact_key,
            settings=settings,
            base_url=f"{request.url.scheme}://{request.url.netloc}",
        )
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    version = ""
    tiles = payload.get("tiles")
    if isinstance(tiles, list) and tiles:
        version = str(tiles[0]).split("?v=", 1)[-1] if "?v=" in str(tiles[0]) else ""
    headers = {"Cache-Control": "public, max-age=300, stale-while-revalidate=86400"}
    if version:
        headers["ETag"] = f'"{version}"'
    return JSONResponse(payload, headers=headers)


@router.get("/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}/tiles/{z}/{x}/{y}.mvt")
def get_milestone_artifact_vector_tile(
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    z: int,
    x: int,
    y: int,
    settings=Depends(get_app_settings),
) -> Response:
    _validate_xyz_bounds(z, x, y)
    try:
        artifact_path, media_type = resolve_temporal_project_artifact_path(
            project_id=project_id,
            release_identifier=release_identifier,
            artifact_key=artifact_key,
            settings=settings,
            access_mode="vector_tile",
        )
        if media_type != "application/geo+json":
            raise FileNotFoundError(f"Temporal artifact is not vector-tile capable: {artifact_key}")
        stat = artifact_path.stat()
        version = f"{stat.st_mtime_ns}-{stat.st_size}"
        cache_path = (
            settings.runtime_cache_dir
            / "temporal_vector_tiles"
            / quote(project_id, safe="")
            / quote(release_identifier, safe="")
            / quote(artifact_key, safe="")
            / quote(version, safe="")
            / str(z)
            / str(x)
            / f"{y}.mvt"
        )
        if cache_path.is_file():
            logger.info(
                "TEMPORAL_MVT_TILE_CACHE_HIT projectId=%s releaseIdentifier=%s artifactKey=%s z=%s x=%s y=%s cachePath=%s",
                project_id,
                release_identifier,
                artifact_key,
                z,
                x,
                y,
                cache_path,
            )
            return FileResponse(
                cache_path,
                media_type="application/vnd.mapbox-vector-tile",
                headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{version}"'},
            )
        logger.info(
            "TEMPORAL_MVT_TILE_CACHE_MISS projectId=%s releaseIdentifier=%s artifactKey=%s z=%s x=%s y=%s cachePath=%s",
            project_id,
            release_identifier,
            artifact_key,
            z,
            x,
            y,
            cache_path,
        )
        content = render_temporal_artifact_vector_tile(
            project_id=project_id,
            release_identifier=release_identifier,
            artifact_key=artifact_key,
            z=z,
            x=x,
            y=y,
            settings=settings,
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
    except FileNotFoundError as exc:
        raise_api_error(status.HTTP_404_NOT_FOUND, "not_found", str(exc))
    except RuntimeError as exc:
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "vector_tile_dependency_missing", str(exc))
    return Response(
        content=content,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{version}"'},
    )


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
    _validate_xyz_bounds(z, x, y)
    metadata_started_at = time.perf_counter()
    _, cog_info = _resolve_milestone_reference_imagery(project_id, release_identifier, settings=settings)
    metadata_ms = round((time.perf_counter() - metadata_started_at) * 1000, 2)
    tile_cache = ensure_reference_tile_png_cached_on_disk(
        cache_root=settings.reference_tile_cache_dir,
        project_id=project_id,
        release_identifier=release_identifier,
        cog_info=cog_info,
        z=z,
        x=x,
        y=y,
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
    if tile_cache.cache_hit:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.info(
            "TEMPORAL_REFERENCE_TILE_CACHE_HIT projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cachePath=%s",
            project_id,
            release_identifier,
            z,
            x,
            y,
            duration_ms,
            tile_cache.cache_path,
        )
        logger.info(
            "TILE_CACHE_HIT projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cachePath=%s",
            project_id,
            release_identifier,
            z,
            x,
            y,
            duration_ms,
            tile_cache.cache_path,
        )
        logger.info(
            "TILE_SERVED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cacheHeaders=%s cachePath=%s cacheHit=true",
            project_id,
            release_identifier,
            z,
            x,
            y,
            duration_ms,
            "public, max-age=31536000, immutable",
            tile_cache.cache_path,
        )
        return FileResponse(
            tile_cache.cache_path,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{reference_imagery_version_token(cog_info)}"',
            },
        )

    result = tile_cache.render_result
    if result is None:
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "tile_render_failed", "Reference tile rendering failed.")
    response = Response(
        content=result.content,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{reference_imagery_version_token(cog_info)}"',
        },
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
    if tile_cache.cache_write_succeeded:
        logger.info(
            "TILE_CACHE_WRITE projectId=%s releaseIdentifier=%s z=%s x=%s y=%s cachePath=%s",
            project_id,
            release_identifier,
            z,
            x,
            y,
            tile_cache.cache_path,
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
        "TILE_CACHE_MISS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cachePath=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        round((time.perf_counter() - started_at) * 1000, 2),
        tile_cache.cache_path,
    )
    logger.info(
        "TEMPORAL_REFERENCE_TILE_CACHE_MISS projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cachePath=%s alphaSource=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        round((time.perf_counter() - started_at) * 1000, 2),
        tile_cache.cache_path,
        result.alpha_source,
    )
    logger.info(
        "TEMPORAL_REFERENCE_TILE_RGBA_CONFIRMED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s outputPngHasAlpha=%s alphaSource=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.output_png_has_alpha,
        result.alpha_source,
    )
    logger.info(
        "TEMPORAL_REFERENCE_TILE_NODATA_ALPHA_CONFIRMED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s transparentPixelCount=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.transparent_pixel_count,
    )
    logger.info(
        "TEMPORAL_REFERENCE_TILE_VALID_ALPHA_CONFIRMED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s opaquePixelCount=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        result.opaque_pixel_count,
    )
    logger.info(
        "TEMPORAL_REFERENCE_TILE_RESPONSE projectId=%s releaseIdentifier=%s z=%s x=%s y=%s durationMs=%s cachePath=%s cacheHit=false cacheHeaders=%s",
        project_id,
        release_identifier,
        z,
        x,
        y,
        round((time.perf_counter() - started_at) * 1000, 2),
        tile_cache.cache_path,
        response.headers.get("Cache-Control"),
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


@router.post("/{project_id}/milestones/{release_identifier}/reference/prewarm")
def prewarm_reference_tiles(
    project_id: str,
    release_identifier: str,
    body: ReferenceTilePrewarmRequest,
    settings=Depends(get_app_settings),
) -> dict[str, int | float]:
    started_at = time.perf_counter()
    if len(body.tiles) > settings.reference_tile_prewarm_max_tiles:
        raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "too_many_tiles",
            f"Reference tile prewarm accepts at most {settings.reference_tile_prewarm_max_tiles} tiles.",
        )
    for tile in body.tiles:
        _validate_xyz_bounds(tile.z, tile.x, tile.y)
    logger.info(
        "REFERENCE_TILE_PREWARM_START projectId=%s releaseIdentifier=%s requested=%s",
        project_id,
        release_identifier,
        len(body.tiles),
    )
    _, cog_info = _resolve_milestone_reference_imagery(project_id, release_identifier, settings=settings)
    hits = 0
    misses = 0
    generated = 0
    failed = 0
    for tile in body.tiles:
        try:
            result = ensure_reference_tile_png_cached_on_disk(
                cache_root=settings.reference_tile_cache_dir,
                project_id=project_id,
                release_identifier=release_identifier,
                cog_info=cog_info,
                z=tile.z,
                x=tile.x,
                y=tile.y,
            )
            if result.cache_hit:
                hits += 1
            else:
                misses += 1
                generated += 1
                if result.cache_write_succeeded:
                    logger.info(
                        "TILE_CACHE_WRITE projectId=%s releaseIdentifier=%s z=%s x=%s y=%s cachePath=%s prewarm=true",
                        project_id,
                        release_identifier,
                        tile.z,
                        tile.x,
                        tile.y,
                        result.cache_path,
                    )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning(
                "REFERENCE_TILE_PREWARM_TILE_FAILED projectId=%s releaseIdentifier=%s z=%s x=%s y=%s error=%s",
                project_id,
                release_identifier,
                tile.z,
                tile.x,
                tile.y,
                exc,
            )
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "REFERENCE_TILE_PREWARM_DONE projectId=%s releaseIdentifier=%s requested=%s hits=%s misses=%s generated=%s failed=%s durationMs=%s",
        project_id,
        release_identifier,
        len(body.tiles),
        hits,
        misses,
        generated,
        failed,
        duration_ms,
    )
    return {
        "requested": len(body.tiles),
        "hits": hits,
        "misses": misses,
        "generated": generated,
        "failed": failed,
        "durationMs": duration_ms,
    }


@router.get("/{project_id}/reference-layers")
def list_project_reference_layers(project_id: str, settings=Depends(get_app_settings)) -> list[ReferenceLayer]:
    started_at = time.perf_counter()
    try:
        read_started_at = time.perf_counter()
        layers = list_reference_layers(project_id, settings)
        logger.info(
            "REFERENCE_LAYERS_TIMING projectId=%s phase=manual_registry_read ms=%s",
            project_id,
            round((time.perf_counter() - read_started_at) * 1000, 2),
        )
        logger.info(
            "REFERENCE_LAYERS_LISTED projectId=%s count=%s durationMs=%s",
            project_id,
            len(layers),
            round((time.perf_counter() - started_at) * 1000, 2),
        )
        logger.info(
            "REFERENCE_LAYERS_TIMING projectId=%s phase=response ms=%s",
            project_id,
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
    except ValueError as exc:
        logging.getLogger(__name__).exception("TEMPORAL_QGIS_EXPORT_FAILED projectId=%s error=%s", project_id, exc)
        raise_api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "temporal_qgis_export_failed", str(exc))
    return {
        "path": str(bundle_path),
        "filename": bundle_path.name,
        "label": "Export QGIS",
    }
