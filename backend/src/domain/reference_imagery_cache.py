from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Literal

from src.domain.raster_write_options import validate_geotiff_file


REFERENCE_IMAGERY_CACHE_KEY_VERSION = 1
REFERENCE_IMAGERY_COG_FILENAME = "reference_imagery_cog.tif"
REFERENCE_IMAGERY_METADATA_FILENAME = "metadata.json"
MaterializationMode = Literal["hardlink", "symlink", "copy"]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_aoi_hash(aoi_geojson: dict[str, object] | None) -> str | None:
    if aoi_geojson is None:
        return None
    return hashlib.sha256(_canonical_json(aoi_geojson).encode("utf-8")).hexdigest()


def build_reference_imagery_cache_key_payload(
    *,
    provider: str,
    release_identifier: str,
    release_num: int | None,
    tile_matrix_set: str | None,
    zoom: int | None,
    tile_range: object | None,
    bounds_3857: object | None,
    source_raster_path: Path | None,
    valid_mask_path: Path | None,
    aoi_hash: str | None,
    reference_cog_format_version: int,
) -> dict[str, object]:
    # Source files are intentionally not part of the cache key. The same Wayback
    # mosaic can be staged in different request/project folders, and those
    # project-specific paths must still map to one canonical reference COG.
    return {
        "key_version": REFERENCE_IMAGERY_CACHE_KEY_VERSION,
        "provider": provider,
        "release_identifier": release_identifier,
        "release_num": release_num,
        "tile_matrix_set": tile_matrix_set,
        "zoom": zoom,
        "tile_range": tile_range,
        "bounds_3857": bounds_3857,
        "aoi_hash": aoi_hash,
        "reference_cog_format_version": reference_cog_format_version,
    }


def build_reference_imagery_key(payload: dict[str, object]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"refimg-v{REFERENCE_IMAGERY_CACHE_KEY_VERSION}-{digest}"


def reference_imagery_cache_entry_dir(cache_dir: Path, reference_imagery_key: str) -> Path:
    return cache_dir / reference_imagery_key


def reference_imagery_cache_cog_path(cache_dir: Path, reference_imagery_key: str) -> Path:
    return reference_imagery_cache_entry_dir(cache_dir, reference_imagery_key) / REFERENCE_IMAGERY_COG_FILENAME


def reference_imagery_cache_metadata_path(cache_dir: Path, reference_imagery_key: str) -> Path:
    return reference_imagery_cache_entry_dir(cache_dir, reference_imagery_key) / REFERENCE_IMAGERY_METADATA_FILENAME


def read_reference_imagery_cache_metadata(metadata_path: Path) -> dict[str, Any] | None:
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_reference_imagery_cache_metadata(metadata_path: Path, metadata: dict[str, Any]) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, metadata_path)


def valid_existing_canonical_cog(canonical_cog_path: Path, *, reference_imagery_key: str | None = None) -> bool:
    if not canonical_cog_path.is_file():
        return False
    if canonical_cog_path.stat().st_size <= 0:
        return False
    try:
        validate_geotiff_file(
            canonical_cog_path,
            min_band_count=4,
        )
    except Exception:
        return False
    if reference_imagery_key is None:
        return True
    metadata = read_reference_imagery_cache_metadata(canonical_cog_path.with_name(REFERENCE_IMAGERY_METADATA_FILENAME))
    return metadata is not None and metadata.get("reference_imagery_key") == reference_imagery_key


def build_reference_imagery_cache_metadata(
    *,
    reference_imagery_key: str,
    key_payload: dict[str, object],
    canonical_cog_path: Path,
    existing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utc_now_iso()
    canonical_stat = canonical_cog_path.stat()
    metadata: dict[str, Any] = {
        "reference_imagery_key": reference_imagery_key,
        "key_version": REFERENCE_IMAGERY_CACHE_KEY_VERSION,
        "created_at": existing_metadata.get("created_at") if existing_metadata else now,
        "updated_at": now,
        "canonical_cog_path": str(canonical_cog_path),
        "canonical_cog_size": canonical_stat.st_size,
        "canonical_cog_mtime_ns": canonical_stat.st_mtime_ns,
        "materializations": list(existing_metadata.get("materializations", [])) if existing_metadata else [],
    }
    metadata.update(key_payload)
    return metadata


def append_reference_imagery_materialization(
    metadata: dict[str, Any],
    *,
    project_id: str,
    release_identifier: str,
    project_cog_path: Path,
    method: str,
) -> dict[str, Any]:
    materializations = list(metadata.get("materializations", []))
    materializations = [
        item
        for item in materializations
        if not (
            isinstance(item, dict)
            and item.get("project_id") == project_id
            and item.get("release_identifier") == release_identifier
            and item.get("project_cog_path") == str(project_cog_path)
        )
    ]
    materializations.append(
        {
            "project_id": project_id,
            "release_identifier": release_identifier,
            "project_cog_path": str(project_cog_path),
            "method": method,
            "created_at": _utc_now_iso(),
        }
    )
    metadata["materializations"] = materializations
    metadata["updated_at"] = _utc_now_iso()
    return metadata


def _remove_existing_project_cog(project_cog_path: Path) -> None:
    if not project_cog_path.exists() and not project_cog_path.is_symlink():
        return
    if project_cog_path.is_dir() and not project_cog_path.is_symlink():
        raise IsADirectoryError(f"Cannot replace reference imagery COG directory: {project_cog_path}")
    project_cog_path.unlink()


def _try_hardlink(canonical_cog_path: Path, project_cog_path: Path) -> str:
    os.link(canonical_cog_path, project_cog_path)
    return "hardlink"


def _try_symlink(canonical_cog_path: Path, project_cog_path: Path) -> str:
    project_cog_path.symlink_to(canonical_cog_path)
    return "symlink"


def _try_copy(canonical_cog_path: Path, project_cog_path: Path) -> str:
    shutil.copy2(canonical_cog_path, project_cog_path)
    return "copy"


def materialize_reference_imagery_cog(
    *,
    canonical_cog_path: Path,
    project_cog_path: Path,
    mode: MaterializationMode = "hardlink",
) -> dict[str, object]:
    canonical_cog_path = canonical_cog_path.expanduser().resolve()
    project_cog_path = project_cog_path.expanduser().resolve()
    if not canonical_cog_path.is_file():
        raise FileNotFoundError(f"Canonical reference imagery COG is missing: {canonical_cog_path}")
    if canonical_cog_path.stat().st_size <= 0:
        raise ValueError(f"Canonical reference imagery COG is empty: {canonical_cog_path}")

    project_cog_path.parent.mkdir(parents=True, exist_ok=True)
    if project_cog_path.exists() or project_cog_path.is_symlink():
        try:
            if project_cog_path.samefile(canonical_cog_path):
                return {
                    "method": "existing",
                    "project_cog_path": str(project_cog_path),
                    "canonical_cog_path": str(canonical_cog_path),
                }
        except OSError:
            pass
        _remove_existing_project_cog(project_cog_path)

    attempts: list[MaterializationMode] = [mode]
    for fallback in ("symlink", "copy"):
        if fallback not in attempts:
            attempts.append(fallback)  # type: ignore[arg-type]

    errors: list[str] = []
    for attempt in attempts:
        try:
            if attempt == "hardlink":
                method = _try_hardlink(canonical_cog_path, project_cog_path)
            elif attempt == "symlink":
                method = _try_symlink(canonical_cog_path, project_cog_path)
            else:
                method = _try_copy(canonical_cog_path, project_cog_path)
            return {
                "method": method,
                "project_cog_path": str(project_cog_path),
                "canonical_cog_path": str(canonical_cog_path),
                "errors": errors,
            }
        except OSError as exc:
            errors.append(f"{attempt}:{exc.__class__.__name__}:{exc}")
            if project_cog_path.exists() or project_cog_path.is_symlink():
                _remove_existing_project_cog(project_cog_path)

    raise OSError(f"Unable to materialize reference imagery COG at {project_cog_path}: {errors}")
