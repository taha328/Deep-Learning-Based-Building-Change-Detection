from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import rasterio
from fastapi import UploadFile
from osgeo import ogr, osr
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from src.config import Settings
from src.schemas import (
    ReferenceLayer,
    ReferenceLayerPatchRequest,
    ReferenceLayerPreflightResponse,
    ReferenceLayerStyle,
)
from src.services.temporal_projects import get_temporal_project


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
VECTOR_EXTENSIONS = {".geojson", ".json", ".gpkg", ".kml", ".kmz", ".gpx", ".shz"}
RASTER_EXTENSIONS = {".tif", ".tiff"}
ARCHIVE_EXTENSIONS = {".zip", ".shz", ".kmz"}
ALLOWED_EXTENSIONS = VECTOR_EXTENSIONS | RASTER_EXTENSIONS | ARCHIVE_EXTENSIONS
ARCHIVE_VECTOR_WHITELIST = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".aih", ".ain", ".fix", ".xml"}
MAX_ARCHIVE_FILES = 128


class ReferenceLayerError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class PmtilesToolStatus:
    available: bool
    tippecanoe_available: bool
    pmtiles_available: bool
    tippecanoe_path: str | None
    pmtiles_path: str | None
    reason: str | None

    def to_payload(self) -> dict[str, str]:
        return {
            "pmtiles": "available" if self.available else "unavailable",
            "tippecanoe": self.tippecanoe_path or "missing",
            "pmtiles_cli": self.pmtiles_path or "missing",
            "reason": self.reason or "",
        }


@dataclass
class PreparedVectorInput:
    dataset_path: Path
    original_format: str
    cleanup_dir: Path | None
    warnings: list[str]


@dataclass
class VectorInspection:
    payload: dict[str, Any]
    geometries: list[BaseGeometry]
    geometry_type: str
    crs: str
    bounds_wgs84: list[float]
    feature_count: int
    original_format: str
    warnings: list[str]


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_project_id(project_id: str) -> str:
    if not PROJECT_ID_PATTERN.match(project_id):
        raise ReferenceLayerError("invalid_project_id", "Invalid project_id.", status_code=400)
    return project_id


def _safe_filename(filename: str) -> str:
    basename = Path(filename or "reference-layer").name
    cleaned = SAFE_FILENAME_PATTERN.sub("_", basename).strip("._")
    return cleaned or "reference-layer"


def _sanitize_source_layer_name(name: str, fallback: str) -> str:
    normalized = SAFE_FILENAME_PATTERN.sub("_", name.strip().lower()).strip("._")
    if not normalized:
        normalized = fallback
    return normalized[:64]


def _extension(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".shp.zip"):
        return ".zip"
    return Path(name).suffix


def _project_dir(settings: Settings, project_id: str) -> Path:
    project = get_temporal_project(_safe_project_id(project_id), settings)
    if project.project_dir:
        return Path(project.project_dir).resolve()
    return (settings.temporal_projects_dir / project.project_id).resolve()


def _reference_layers_dir(settings: Settings, project_id: str) -> Path:
    path = _project_dir(settings, project_id) / "reference_layers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _metadata_path(settings: Settings, project_id: str) -> Path:
    return _reference_layers_dir(settings, project_id) / "reference_layers.json"


def _layer_dir(settings: Settings, project_id: str, layer_id: str) -> Path:
    safe_layer_id = _safe_filename(layer_id)
    path = _reference_layers_dir(settings, project_id) / safe_layer_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_url(path: str | None) -> str | None:
    if not path:
        return None
    return f"/api/files?path={quote(path, safe='')}"


def _public_layer(layer: ReferenceLayer) -> ReferenceLayer:
    payload = layer.model_copy(deep=True)
    payload.source_path = None
    payload.display_path = None
    payload.display_url = _file_url(layer.display_path)
    payload.pmtiles_url = _file_url(layer.display_path) if layer.storage_strategy == "pmtiles" else None
    return payload


def _read_metadata(settings: Settings, project_id: str) -> list[ReferenceLayer]:
    path = _metadata_path(settings, project_id)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    layers: list[ReferenceLayer] = []
    for item in payload:
        if isinstance(item, dict):
            layers.append(ReferenceLayer.model_validate(item))
    return layers


def _write_metadata(settings: Settings, project_id: str, layers: list[ReferenceLayer]) -> None:
    path = _metadata_path(settings, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump([layer.model_dump(mode="json") for layer in layers], handle, indent=2)
        handle.flush()
    tmp_path.replace(path)


def _write_geojson_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
    tmp_path.replace(path)


async def _save_upload_to_tmp(upload: UploadFile, settings: Settings) -> tuple[Path, int]:
    filename = _safe_filename(upload.filename or "reference-layer")
    suffix = _extension(filename)
    if suffix not in ALLOWED_EXTENSIONS:
        raise ReferenceLayerError("unsupported_format", f"Unsupported reference layer format: {suffix or 'unknown'}.")
    tmp_dir = settings.tmp_cache_dir / "reference_layers"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}_{filename}"
    size = 0
    try:
        with tmp_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.reference_layer_max_upload_bytes:
                    raise ReferenceLayerError("upload_too_large", "Reference layer upload exceeds the configured size limit.")
                handle.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.seek(0)
    return tmp_path, size


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


def _validate_zip(path: Path, *, max_total_size: int) -> list[zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ReferenceLayerError("archive_too_large", "Archive contains too many files.")
            total_size = 0
            for entry in infos:
                entry_path = Path(entry.filename)
                if entry_path.is_absolute() or ".." in entry_path.parts:
                    raise ReferenceLayerError("unsafe_archive", "Archive contains an unsafe path.")
                if _zip_is_symlink(entry):
                    raise ReferenceLayerError("unsafe_archive", "Archive contains a symbolic link, which is not allowed.")
                total_size += entry.file_size
                if total_size > max_total_size:
                    raise ReferenceLayerError("archive_too_large", "Archive contents exceed the configured size limit.")
            return infos
    except ReferenceLayerError:
        raise
    except zipfile.BadZipFile as exc:
        raise ReferenceLayerError("invalid_archive", "Uploaded ZIP is not a valid archive.") from exc


def _safe_extract_archive(
    archive_path: Path,
    output_dir: Path,
    *,
    allowed_suffixes: set[str],
    max_total_size: int,
) -> list[Path]:
    infos = _validate_zip(archive_path, max_total_size=max_total_size)
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted_paths: list[Path] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in infos:
            if info.is_dir():
                continue
            suffix = Path(info.filename).suffix.lower()
            if suffix not in allowed_suffixes:
                continue
            target = (output_dir / Path(info.filename).name).resolve()
            if output_dir.resolve() not in target.parents and target != output_dir.resolve():
                raise ReferenceLayerError("unsafe_archive", "Archive extraction resolved outside the approved temp directory.")
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted_paths.append(target)
    return extracted_paths


def _validate_shapefile_parts(paths: list[Path]) -> Path:
    shp_files = [path for path in paths if path.suffix.lower() == ".shp"]
    if len(shp_files) > 1:
        raise ReferenceLayerError("multiple_layers_not_supported", "Multiple vector layers found; layer selection is not implemented yet.")
    if len(shp_files) != 1:
        raise ReferenceLayerError("missing_shapefile", "Archive does not contain a usable .shp layer.")
    shp_path = shp_files[0]
    stem_paths = {path.suffix.lower() for path in paths if path.stem == shp_path.stem}
    for required in (".shx", ".dbf", ".prj"):
        if required not in stem_paths:
            raise ReferenceLayerError("missing_required_sidecar", f"Shapefile archive is missing the required {required} sidecar.")
    return shp_path


def _prepare_vector_input(path: Path, settings: Settings) -> PreparedVectorInput:
    name_lower = path.name.lower()
    cleanup_dir: Path | None = None
    warnings: list[str] = []
    if name_lower.endswith(".geojson") or name_lower.endswith(".json"):
        return PreparedVectorInput(path, "geojson", None, warnings)
    if name_lower.endswith(".gpkg"):
        return PreparedVectorInput(path, "gpkg", None, warnings)
    if name_lower.endswith(".kml"):
        return PreparedVectorInput(path, "kml", None, warnings)
    if name_lower.endswith(".gpx"):
        return PreparedVectorInput(path, "gpx", None, warnings)
    if name_lower.endswith(".kmz"):
        cleanup_dir = settings.tmp_cache_dir / "reference_layers" / f"extract_{uuid.uuid4().hex}"
        extracted = _safe_extract_archive(path, cleanup_dir, allowed_suffixes={".kml"}, max_total_size=settings.reference_layer_max_upload_bytes)
        kml_files = [item for item in extracted if item.suffix.lower() == ".kml"]
        if len(kml_files) != 1:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
            raise ReferenceLayerError("invalid_kmz", "KMZ archives must contain exactly one .kml file.")
        return PreparedVectorInput(kml_files[0], "kmz", cleanup_dir, warnings)
    if name_lower.endswith(".zip") or name_lower.endswith(".shz"):
        cleanup_dir = settings.tmp_cache_dir / "reference_layers" / f"extract_{uuid.uuid4().hex}"
        extracted = _safe_extract_archive(path, cleanup_dir, allowed_suffixes=ARCHIVE_VECTOR_WHITELIST, max_total_size=settings.reference_layer_max_upload_bytes)
        shp_path = _validate_shapefile_parts(extracted)
        return PreparedVectorInput(shp_path, "shapefile_zip" if name_lower.endswith(".zip") else "shz", cleanup_dir, warnings)
    raise ReferenceLayerError("unsupported_format", f"Unsupported reference layer format: {_extension(path.name) or 'unknown'}.")


def _feature_geometry_type(geometry: BaseGeometry) -> str:
    geom_type = geometry.geom_type.lower()
    if geom_type in {"point", "multipoint"}:
        return "point"
    if geom_type in {"linestring", "multilinestring", "linearring"}:
        return "line"
    if geom_type in {"polygon", "multipolygon"}:
        return "polygon"
    return "mixed"


def _bounds_from_geometries(geometries: list[BaseGeometry]) -> list[float]:
    minx = min(geometry.bounds[0] for geometry in geometries)
    miny = min(geometry.bounds[1] for geometry in geometries)
    maxx = max(geometry.bounds[2] for geometry in geometries)
    maxy = max(geometry.bounds[3] for geometry in geometries)
    return [float(minx), float(miny), float(maxx), float(maxy)]


def _combined_geometry_type(geometries: list[BaseGeometry]) -> str:
    families = {_feature_geometry_type(geometry) for geometry in geometries if not geometry.is_empty}
    families.discard("mixed")
    if not families:
        return "mixed"
    if len(families) == 1:
        return next(iter(families))
    return "mixed"


def _load_geojson_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReferenceLayerError("invalid_geojson", "Uploaded GeoJSON could not be parsed.") from exc
    if not isinstance(payload, dict):
        raise ReferenceLayerError("invalid_geojson", "Uploaded GeoJSON must be an object.")
    return payload


def _spatial_ref_to_crs(spatial_ref: osr.SpatialReference | None, original_format: str) -> str | None:
    if spatial_ref is None:
        if original_format in {"geojson", "kml", "kmz", "gpx"}:
            return "EPSG:4326"
        return None
    spatial_ref.AutoIdentifyEPSG()
    authority_name = spatial_ref.GetAuthorityName(None)
    authority_code = spatial_ref.GetAuthorityCode(None)
    if authority_name and authority_code:
        return f"{authority_name}:{authority_code}"
    return spatial_ref.ExportToProj4() or spatial_ref.ExportToWkt()


def _read_vector_dataset(path: Path, original_format: str) -> VectorInspection:
    datasource = ogr.Open(str(path), 0)
    if datasource is None:
        raise ReferenceLayerError("invalid_vector", "Uploaded vector layer could not be opened.")

    warnings: list[str] = []
    normalized_features: list[dict[str, Any]] = []
    geometries: list[BaseGeometry] = []

    layer_count = datasource.GetLayerCount()
    if layer_count > 1:
        warnings.append("Multiple vector sublayers were merged into a single reference layer.")

    for layer_index in range(layer_count):
        layer = datasource.GetLayerByIndex(layer_index)
        if layer is None:
            continue
        spatial_ref = layer.GetSpatialRef()
        crs = _spatial_ref_to_crs(spatial_ref, original_format)
        if crs is None:
            raise ReferenceLayerError("missing_crs", "Vector layer CRS is missing; assign CRS before using this layer.")
        needs_transform = crs != "EPSG:4326"
        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True) if needs_transform else None
        definition = layer.GetLayerDefn()
        layer.ResetReading()
        feature = layer.GetNextFeature()
        while feature is not None:
            ogr_geometry = feature.GetGeometryRef()
            if ogr_geometry is not None:
                geometry = shape(json.loads(ogr_geometry.ExportToJson()))
                if not geometry.is_valid:
                    geometry = geometry.buffer(0)
                if not geometry.is_empty:
                    if transformer is not None:
                        geometry = shapely_transform(transformer.transform, geometry)
                    properties: dict[str, Any] = {}
                    for field_index in range(definition.GetFieldCount()):
                        field_name = definition.GetFieldDefn(field_index).GetName()
                        properties[field_name] = feature.GetField(field_index)
                    normalized_features.append(
                        {
                            "type": "Feature",
                            "properties": properties,
                            "geometry": mapping(geometry),
                        }
                    )
                    geometries.append(geometry)
            feature = layer.GetNextFeature()

    if not geometries:
        raise ReferenceLayerError("empty_layer", "Reference layer contains no usable geometries.")

    payload = {"type": "FeatureCollection", "features": normalized_features}
    return VectorInspection(
        payload=payload,
        geometries=geometries,
        geometry_type=_combined_geometry_type(geometries),
        crs="EPSG:4326",
        bounds_wgs84=_bounds_from_geometries(geometries),
        feature_count=len(normalized_features),
        original_format=original_format,
        warnings=warnings,
    )


def _inspect_vector_path(path: Path, file_size: int, scope: str, settings: Settings) -> ReferenceLayerPreflightResponse:
    prepared = _prepare_vector_input(path, settings)
    try:
        inspection = _read_vector_dataset(prepared.dataset_path, prepared.original_format)
        warnings = list(prepared.warnings) + list(inspection.warnings)
        errors: list[str] = []
        tool_status: dict[str, str] = {}
        storage_strategy = "geojson"
        if scope == "full_layer":
            storage_strategy = "pmtiles"
            pmtiles_status = _pmtiles_tool_status(settings)
            tool_status = pmtiles_status.to_payload()
            errors.append("Full-layer vector visualization requires PMTiles support. Use Clip to AOI for now." if not pmtiles_status.available else "")
            errors = [item for item in errors if item]
            if file_size > settings.reference_layer_pmtiles_max_upload_mb * 1024 * 1024:
                errors.append("The uploaded vector file exceeds the configured PMTiles full-layer upload limit.")
            if not pmtiles_status.tippecanoe_available:
                errors.append("Tippecanoe is not installed on the backend, so PMTiles full-layer import is unavailable.")
            if not pmtiles_status.pmtiles_available:
                errors.append("The pmtiles CLI is not installed on the backend, so PMTiles full-layer import is unavailable.")
            if pmtiles_status.available:
                warnings.append("Full-layer vector imports are rendered as PMTiles vector tiles and never returned as raw GeoJSON.")
        else:
            if file_size > settings.reference_layer_large_vector_input_threshold_bytes or inspection.feature_count > 100_000:
                warnings.append("The source dataset is large. Clip to AOI or use Import full layer to build PMTiles vector tiles.")
            elif file_size > settings.reference_layer_browser_geojson_max_bytes or inspection.feature_count > settings.reference_layer_browser_geojson_max_features:
                warnings.append("Large GeoJSON layers are expensive in the browser; use Clip to AOI or a tiled strategy for production-scale data.")

        return ReferenceLayerPreflightResponse(
            original_filename=_safe_filename(path.name),
            original_format=inspection.original_format,
            layer_kind="vector",
            geometry_type=inspection.geometry_type,  # type: ignore[arg-type]
            scope=scope,  # type: ignore[arg-type]
            storage_strategy=storage_strategy,  # type: ignore[arg-type]
            crs=inspection.crs,
            bounds_wgs84=inspection.bounds_wgs84,
            feature_count=inspection.feature_count,
            file_size_bytes=file_size,
            tool_status=tool_status,
            warnings=warnings,
            errors=errors,
        )
    finally:
        if prepared.cleanup_dir is not None:
            shutil.rmtree(prepared.cleanup_dir, ignore_errors=True)


def _inspect_raster(path: Path, file_size: int, scope: str) -> ReferenceLayerPreflightResponse:
    try:
        with rasterio.open(path) as dataset:
            crs = dataset.crs.to_string() if dataset.crs else None
            if crs is None:
                return ReferenceLayerPreflightResponse(
                    original_filename=path.name,
                    original_format="geotiff",
                    layer_kind="raster",
                    geometry_type="raster",
                    scope=scope,  # type: ignore[arg-type]
                    storage_strategy="cog",
                    file_size_bytes=file_size,
                    errors=["Raster CRS is missing; assign CRS before using this layer."],
                )
            bounds = dataset.bounds
            if dataset.crs and dataset.crs.to_string() != "EPSG:4326":
                transformer = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
                xs = [bounds.left, bounds.right, bounds.right, bounds.left]
                ys = [bounds.bottom, bounds.bottom, bounds.top, bounds.top]
                lon, lat = transformer.transform(xs, ys)
                bounds_wgs84 = [float(min(lon)), float(min(lat)), float(max(lon)), float(max(lat))]
            else:
                bounds_wgs84 = [float(bounds.left), float(bounds.bottom), float(bounds.right), float(bounds.top)]
            return ReferenceLayerPreflightResponse(
                original_filename=path.name,
                original_format="geotiff",
                layer_kind="raster",
                geometry_type="raster",
                scope=scope,  # type: ignore[arg-type]
                storage_strategy="cog",
                crs=crs,
                bounds_wgs84=bounds_wgs84,
                feature_count=None,
                file_size_bytes=file_size,
                warnings=["Raster visualization requires COG/raster tile serving and is not enabled in this build."],
                errors=[],
            )
    except Exception as exc:
        raise ReferenceLayerError("invalid_raster", "Uploaded raster could not be inspected.") from exc


def _preflight_path(path: Path, file_size: int, scope: str, settings: Settings) -> ReferenceLayerPreflightResponse:
    suffix = _extension(path.name)
    if suffix in RASTER_EXTENSIONS:
        return _inspect_raster(path, file_size, scope)
    if suffix in ALLOWED_EXTENSIONS:
        return _inspect_vector_path(path, file_size, scope, settings)
    raise ReferenceLayerError("unsupported_format", f"Unsupported reference layer format: {suffix or 'unknown'}.")


async def preflight_reference_layer(
    project_id: str,
    upload: UploadFile,
    *,
    settings: Settings,
    scope: str = "aoi_clipped",
) -> ReferenceLayerPreflightResponse:
    _project_dir(settings, project_id)
    tmp_path, size = await _save_upload_to_tmp(upload, settings)
    try:
        result = _preflight_path(tmp_path, size, scope, settings)
        if upload.filename:
            result.original_filename = _safe_filename(upload.filename)
        return result
    finally:
        tmp_path.unlink(missing_ok=True)


def _clip_payload_to_aoi(
    payload: dict[str, Any],
    project_id: str,
    settings: Settings,
) -> tuple[dict[str, Any], str, list[float], int]:
    project = get_temporal_project(project_id, settings)
    if not project.aoi_geojson:
        raise ReferenceLayerError("missing_aoi", "Project AOI is required before importing a clipped reference layer.")
    aoi = shape(project.aoi_geojson)
    if aoi.is_empty:
        raise ReferenceLayerError("invalid_aoi", "Project AOI is empty.")

    clipped_features: list[dict[str, Any]] = []
    clipped_geometries: list[BaseGeometry] = []
    for feature in payload.get("features", []):
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            continue
        geometry = shape(feature["geometry"])
        clipped = geometry.intersection(aoi)
        if clipped.is_empty:
            continue
        if not clipped.is_valid:
            clipped = clipped.buffer(0)
        if clipped.is_empty:
            continue
        clipped_geometries.append(clipped)
        clipped_features.append(
            {
                "type": "Feature",
                "properties": feature.get("properties") if isinstance(feature.get("properties"), dict) else {},
                "geometry": mapping(clipped),
            }
        )
    display = {"type": "FeatureCollection", "features": clipped_features}
    if not clipped_geometries:
        return display, "mixed", list(aoi.bounds), 0
    return display, _combined_geometry_type(clipped_geometries), _bounds_from_geometries(clipped_geometries), len(clipped_features)


def _resolve_binary(candidate: str) -> str | None:
    if not candidate:
        return None
    path = shutil.which(candidate)
    if path:
        return path
    explicit = Path(candidate)
    if explicit.exists() and explicit.is_file():
        return str(explicit.resolve())
    return None


def _pmtiles_tool_status(settings: Settings) -> PmtilesToolStatus:
    if not settings.reference_layer_pmtiles_enabled:
        return PmtilesToolStatus(False, False, False, None, None, "PMTiles support is disabled by configuration.")
    tippecanoe_path = _resolve_binary(settings.reference_layer_pmtiles_tippecanoe_bin)
    pmtiles_path = _resolve_binary(settings.reference_layer_pmtiles_cli_bin)
    if not tippecanoe_path:
        return PmtilesToolStatus(False, False, bool(pmtiles_path), None, pmtiles_path, "Tippecanoe is not available on this backend.")
    if not pmtiles_path:
        return PmtilesToolStatus(False, True, False, tippecanoe_path, None, "The pmtiles CLI is required to convert internal MBTiles artifacts.")
    return PmtilesToolStatus(True, True, True, tippecanoe_path, pmtiles_path, None)


def _collect_tippecanoe_warnings(stderr: str) -> list[str]:
    warnings: list[str] = []
    for line in stderr.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if "drop" in lowered or "simplif" in lowered or "densest" in lowered:
            warnings.append(normalized[:400])
    return warnings


def _run_command(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReferenceLayerError(
            "pmtiles_build_timeout",
            "PMTiles generation exceeded the configured timeout.",
            details={"command": args[0]},
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        raise ReferenceLayerError(
            "pmtiles_build_failed",
            "PMTiles generation failed.",
            details={"stderr": stderr[:1000], "stdout": stdout[:1000], "command": args[0]},
        ) from exc


def _build_pmtiles_artifact(
    *,
    normalized_geojson_path: Path,
    layer_dir: Path,
    source_layer: str,
    settings: Settings,
) -> tuple[Path, list[str]]:
    pmtiles_status = _pmtiles_tool_status(settings)
    if not pmtiles_status.available or not pmtiles_status.tippecanoe_path or not pmtiles_status.pmtiles_path:
        raise ReferenceLayerError("pmtiles_unavailable", pmtiles_status.reason or "PMTiles tooling is unavailable.")

    build_dir = layer_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    intermediate_mbtiles = build_dir / "layer.mbtiles"
    output_pmtiles = layer_dir / "display" / "layer.pmtiles"
    output_pmtiles.parent.mkdir(parents=True, exist_ok=True)

    tippecanoe_args = [
        pmtiles_status.tippecanoe_path,
        "--force",
        "--layer",
        source_layer,
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "--minimum-zoom",
        str(settings.reference_layer_pmtiles_min_zoom),
        "--maximum-zoom",
        str(settings.reference_layer_pmtiles_max_zoom),
        "--output",
        str(intermediate_mbtiles),
        str(normalized_geojson_path),
    ]
    tippecanoe_result = _run_command(tippecanoe_args, settings.reference_layer_pmtiles_build_timeout_seconds)
    convert_args = [
        pmtiles_status.pmtiles_path,
        "convert",
        str(intermediate_mbtiles),
        str(output_pmtiles),
    ]
    _run_command(convert_args, settings.reference_layer_pmtiles_build_timeout_seconds)

    if not output_pmtiles.exists() or output_pmtiles.stat().st_size <= 0:
        raise ReferenceLayerError("pmtiles_build_failed", "PMTiles generation did not produce a usable output file.")

    warnings = _collect_tippecanoe_warnings(tippecanoe_result.stderr or "")
    if not settings.reference_layer_pmtiles_keep_intermediate:
        shutil.rmtree(build_dir, ignore_errors=True)
    return output_pmtiles, warnings


def _import_reference_layer_sync(
    project_id: str,
    tmp_path: Path,
    *,
    settings: Settings,
    size: int,
    name: str,
    scope: str,
    original_filename: str,
) -> ReferenceLayer:
    preflight = _preflight_path(tmp_path, size, scope, settings)
    if preflight.errors:
        raise ReferenceLayerError("reference_layer_not_importable", preflight.errors[0], details={"errors": preflight.errors})

    layer_id = f"ref-{uuid.uuid4().hex}"
    layer_dir = _layer_dir(settings, project_id, layer_id)
    source_dir = layer_dir / "original"
    display_dir = layer_dir / "display"
    source_dir.mkdir(parents=True, exist_ok=True)
    display_dir.mkdir(parents=True, exist_ok=True)
    safe_original_name = _safe_filename(original_filename or tmp_path.name)
    source_path = source_dir / safe_original_name
    shutil.copy2(tmp_path, source_path)

    warnings = list(preflight.warnings)
    now = _utc_now_iso()

    if preflight.layer_kind == "raster":
        raise ReferenceLayerError(
            "unsupported_strategy",
            "Raster visualization is not enabled in this build. Raster preflight remains available.",
            details={"storage_strategy": preflight.storage_strategy},
        )

    prepared = _prepare_vector_input(source_path, settings)
    try:
        inspection = _read_vector_dataset(prepared.dataset_path, prepared.original_format)
        warnings.extend(prepared.warnings)
        warnings.extend(inspection.warnings)

        if scope == "aoi_clipped":
            display_geojson, geometry_type, bounds, feature_count = _clip_payload_to_aoi(inspection.payload, project_id, settings)
            display_path = display_dir / "layer.geojson"
            _write_geojson_atomic(display_path, display_geojson)
            display_size = display_path.stat().st_size
            visible = True
            if feature_count == 0:
                visible = False
                warnings.append("Layer imported, but no features intersect the current AOI.")
            if display_size > settings.reference_layer_browser_geojson_max_bytes or feature_count > settings.reference_layer_browser_geojson_max_features:
                display_path.unlink(missing_ok=True)
                raise ReferenceLayerError(
                    "display_geojson_too_large",
                    "The clipped layer is still too large for browser GeoJSON display. Use Import full layer to build PMTiles.",
                )
            layer = ReferenceLayer(
                layer_id=layer_id,
                project_id=project_id,
                name=name.strip() or safe_original_name,
                original_filename=safe_original_name,
                original_format=inspection.original_format,
                layer_kind="vector",
                geometry_type=geometry_type,  # type: ignore[arg-type]
                scope="aoi_clipped",
                storage_strategy="geojson",
                crs=inspection.crs,
                bounds_wgs84=[float(value) for value in bounds],
                feature_count=feature_count,
                file_size_bytes=size,
                source_path=str(source_path),
                display_path=str(display_path),
                style=ReferenceLayerStyle(),
                visible=visible,
                opacity=1.0,
                created_at=now,
                updated_at=now,
                warnings=warnings,
            )
        else:
            source_layer = _sanitize_source_layer_name(name or safe_original_name, settings.reference_layer_pmtiles_default_layer_name)
            normalized_geojson_path = layer_dir / "build_input.geojson"
            _write_geojson_atomic(normalized_geojson_path, inspection.payload)
            display_path, build_warnings = _build_pmtiles_artifact(
                normalized_geojson_path=normalized_geojson_path,
                layer_dir=layer_dir,
                source_layer=source_layer,
                settings=settings,
            )
            warnings.extend(build_warnings)
            normalized_geojson_path.unlink(missing_ok=True)
            layer = ReferenceLayer(
                layer_id=layer_id,
                project_id=project_id,
                name=name.strip() or safe_original_name,
                original_filename=safe_original_name,
                original_format=inspection.original_format,
                layer_kind="vector",
                geometry_type=inspection.geometry_type,
                scope="full_layer",
                storage_strategy="pmtiles",
                crs=inspection.crs,
                bounds_wgs84=inspection.bounds_wgs84,
                feature_count=inspection.feature_count,
                file_size_bytes=size,
                source_path=str(source_path),
                display_path=str(display_path),
                source_layer=source_layer,
                style=ReferenceLayerStyle(),
                visible=True,
                opacity=1.0,
                created_at=now,
                updated_at=now,
                warnings=warnings,
            )

        layers = _read_metadata(settings, project_id)
        layers.append(layer)
        _write_metadata(settings, project_id, layers)
        return _public_layer(layer)
    finally:
        if prepared.cleanup_dir is not None:
            shutil.rmtree(prepared.cleanup_dir, ignore_errors=True)


async def import_reference_layer(
    project_id: str,
    upload: UploadFile,
    *,
    settings: Settings,
    name: str,
    scope: str = "aoi_clipped",
    rendering_strategy: str = "auto",
) -> ReferenceLayer:
    del rendering_strategy
    tmp_path, size = await _save_upload_to_tmp(upload, settings)
    try:
        return await asyncio.to_thread(
            _import_reference_layer_sync,
            project_id,
            tmp_path,
            settings=settings,
            size=size,
            name=name,
            scope=scope,
            original_filename=upload.filename or tmp_path.name,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def list_reference_layers(project_id: str, settings: Settings) -> list[ReferenceLayer]:
    return [_public_layer(layer) for layer in _read_metadata(settings, project_id)]


def get_reference_layer(project_id: str, layer_id: str, settings: Settings) -> ReferenceLayer:
    for layer in _read_metadata(settings, project_id):
        if layer.layer_id == layer_id:
            return _public_layer(layer)
    raise ReferenceLayerError("reference_layer_not_found", "Reference layer not found.", status_code=404)


def update_reference_layer(project_id: str, layer_id: str, patch: ReferenceLayerPatchRequest, settings: Settings) -> ReferenceLayer:
    layers = _read_metadata(settings, project_id)
    for index, layer in enumerate(layers):
        if layer.layer_id != layer_id:
            continue
        updated = layer.model_copy(deep=True)
        if patch.name is not None:
            updated.name = patch.name.strip() or updated.name
        if patch.visible is not None:
            updated.visible = patch.visible
        if patch.opacity is not None:
            updated.opacity = max(0.0, min(1.0, patch.opacity))
        if patch.style is not None:
            updated.style = patch.style
        updated.updated_at = _utc_now_iso()
        layers[index] = updated
        _write_metadata(settings, project_id, layers)
        return _public_layer(updated)
    raise ReferenceLayerError("reference_layer_not_found", "Reference layer not found.", status_code=404)


def delete_reference_layer(project_id: str, layer_id: str, settings: Settings) -> None:
    layers = _read_metadata(settings, project_id)
    next_layers = [layer for layer in layers if layer.layer_id != layer_id]
    if len(next_layers) == len(layers):
        raise ReferenceLayerError("reference_layer_not_found", "Reference layer not found.", status_code=404)
    _write_metadata(settings, project_id, next_layers)
    layer_path = _reference_layers_dir(settings, project_id) / _safe_filename(layer_id)
    shutil.rmtree(layer_path, ignore_errors=True)
