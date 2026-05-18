from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal


ArtifactType = Literal["final", "preview", "source", "temp", "export", "metadata"]
KeepPolicy = Literal["always", "debug_only", "cache", "on_demand"]
ArtifactStorage = Literal["request", "shared_cache", "tmp", "external"]

MANIFEST_FILENAME = "manifest.json"
MANIFEST_TMP_FILENAME = "manifest.json.tmp"

_SOURCE_REQUEST_FILE_RE = re.compile(r"^(t1|t2|source)_.+_z\d+(_valid_mask)?\.tif$")
_REQUEST_VALID_MASK_RE = re.compile(r".+_valid_mask\.tif$")
_PREVIEW_FILE_RE = re.compile(r".+_preview\.png$")
_WAYBACK_RGB_SOURCE_FILE_NAMES = {
    "t1_wayback_rgb.tif",
    "t2_wayback_rgb.tif",
    "source_wayback_rgb.tif",
}
_TEMP_FILE_NAMES = {
    "bandon_input_t1.png",
    "bandon_input_t2.png",
    "t1_invalid_mask_for_arosics.tif",
    "t2_invalid_mask_for_arosics.tif",
    "t1_coregistered_to_t2.tif",
    "t1_valid_mask_coregistered_to_t2.tif",
    "run_metadata.json",
    "change_probability.npy",
    "change_mask.png",
}
_FINAL_FILE_NAMES = {
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


def _manifest_path(request_dir: Path) -> Path:
    return request_dir / MANIFEST_FILENAME


def _manifest_tmp_path(request_dir: Path) -> Path:
    return request_dir / MANIFEST_TMP_FILENAME


def _tmp_dir_for_run(request_dir: Path, run_id: str) -> Path:
    return request_dir.parents[1] / "tmp" / run_id


def _storage_for_path(path: Path, request_dir: Path, tmp_dir: Path) -> ArtifactStorage:
    resolved = path.resolve()
    request_root = request_dir.resolve()
    if resolved == request_root or request_root in resolved.parents:
        return "request"
    if tmp_dir.exists():
        tmp_root = tmp_dir.resolve()
        if resolved == tmp_root or tmp_root in resolved.parents:
            return "tmp"
    shared_root = request_dir.parents[1] / "wayback_mosaics"
    if shared_root.exists():
        shared_root = shared_root.resolve()
        if resolved == shared_root or shared_root in resolved.parents:
            return "shared_cache"
    return "external"


def _classify_path(path: Path) -> tuple[ArtifactType, KeepPolicy, bool, str]:
    name = path.name
    if name == MANIFEST_FILENAME:
        return "temp", "debug_only", False, "artifact manifest"
    if name in {"timing.json", "export_timing.json"}:
        return "metadata", "always", False, "Pipeline stage timing report"
    if name == "run_response.json":
        return "source", "cache", False, "cached response payload"
    if name == "export_bundle.zip":
        return "export", "on_demand", False, "export bundle"
    if name in _TEMP_FILE_NAMES:
        return "temp", "debug_only", False, "intermediate pipeline artifact"
    if name in _WAYBACK_RGB_SOURCE_FILE_NAMES:
        return "source", "cache", False, "source raster"
    if name.endswith("_valid_mask.tif"):
        return "source", "cache", False, "source valid mask"
    if _REQUEST_VALID_MASK_RE.fullmatch(name) and _SOURCE_REQUEST_FILE_RE.fullmatch(name):
        return "source", "cache", False, "source valid mask"
    if _SOURCE_REQUEST_FILE_RE.fullmatch(name):
        return "source", "cache", False, "source raster"
    if _PREVIEW_FILE_RE.fullmatch(name):
        return "preview", "always", True, "preview image"
    if name.startswith("building_block_buffer_") or name.startswith("building_change_buffer_"):
        return "final", "always", True, "buffer output"
    if name in _FINAL_FILE_NAMES:
        return "final", "always", True, "final output"
    if name.endswith(".csv") or name.endswith(".geojson") or name.endswith(".tif"):
        return "final", "always", True, "final output"
    return "source", "cache", False, "non-exportable runtime artifact"


def register_artifact(
    *,
    path: Path | str,
    artifact_type: ArtifactType,
    purpose: str,
    format: str,
    keep_policy: KeepPolicy,
    include_in_export: bool,
    storage: ArtifactStorage | None = None,
    request_dir: Path | None = None,
    run_id: str | None = None,
    cache_key: str | None = None,
    resolved_path: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path_obj = Path(path)
    if resolved_path is None:
        resolved_obj = path_obj.resolve()
    else:
        resolved_obj = Path(resolved_path)
    entry: dict[str, Any] = {
        "path": str(path_obj),
        "resolved_path": str(resolved_obj),
        "artifact_type": artifact_type,
        "purpose": purpose,
        "format": format,
        "keep_policy": keep_policy,
        "include_in_export": include_in_export,
    }
    if request_dir is not None and run_id is not None:
        tmp_dir = _tmp_dir_for_run(request_dir.resolve(), run_id)
        entry["storage"] = storage or _storage_for_path(resolved_obj, request_dir.resolve(), tmp_dir)
    elif storage is not None:
        entry["storage"] = storage
    if cache_key:
        entry["cache_key"] = cache_key
    if metadata:
        entry["metadata"] = metadata
    if resolved_obj.exists() and resolved_obj.is_file():
        entry["size_bytes"] = resolved_obj.stat().st_size
    return entry


def write_manifest_atomic(request_dir: Path, manifest: dict[str, Any]) -> Path:
    tmp_path = _manifest_tmp_path(request_dir)
    final_path = _manifest_path(request_dir)
    tmp_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp_path.replace(final_path)
    return final_path


def write_manifest(request_dir: Path, manifest: dict[str, Any]) -> Path:
    return write_manifest_atomic(request_dir, manifest)


def read_manifest(request_dir: Path) -> dict[str, Any] | None:
    path = _manifest_path(request_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_artifact_path(request_dir: Path, artifact: dict[str, Any]) -> Path:
    raw_resolved_path = artifact.get("resolved_path")
    if isinstance(raw_resolved_path, str) and raw_resolved_path:
        return Path(raw_resolved_path)
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Artifact entry is missing path information.")
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (request_dir / path).resolve()


def build_manifest(
    run_id: str,
    request_dir: Path,
    artifacts: list[dict[str, Any]],
    *,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_dir = request_dir.resolve()
    tmp_dir = _tmp_dir_for_run(request_dir, run_id)
    entries: dict[str, dict[str, Any]] = {}

    def add_entry_from_path(candidate: Path, *, cache_key: str | None = None) -> None:
        resolved = candidate.resolve() if candidate.exists() else candidate
        key = str(resolved)
        if key in entries:
            return
        artifact_type, keep_policy, include_in_export, purpose = _classify_path(candidate)
        entries[key] = register_artifact(
            path=resolved,
            resolved_path=resolved,
            artifact_type=artifact_type,
            purpose=purpose,
            format=candidate.suffix.lower().lstrip(".") or "directory",
            keep_policy=keep_policy,
            include_in_export=include_in_export,
            request_dir=request_dir,
            run_id=run_id,
            cache_key=cache_key,
        )

    def add_entry_from_payload(payload: dict[str, Any]) -> None:
        raw_path = payload.get("resolved_path") or payload.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return
        resolved = Path(raw_path)
        key = str(resolved.resolve() if resolved.exists() else resolved)
        if key in entries:
            return
        artifact_type = payload.get("artifact_type")
        keep_policy = payload.get("keep_policy")
        include_in_export = payload.get("include_in_export")
        purpose = payload.get("purpose")
        format_name = payload.get("format")
        if not (
            isinstance(artifact_type, str)
            and isinstance(keep_policy, str)
            and isinstance(include_in_export, bool)
            and isinstance(purpose, str)
            and isinstance(format_name, str)
        ):
            add_entry_from_path(Path(raw_path))
            return
        entries[key] = register_artifact(
            path=Path(payload.get("path", raw_path)),
            resolved_path=resolved,
            artifact_type=artifact_type,  # type: ignore[arg-type]
            purpose=purpose,
            format=format_name,
            keep_policy=keep_policy,  # type: ignore[arg-type]
            include_in_export=include_in_export,
            storage=payload.get("storage") if isinstance(payload.get("storage"), str) else None,  # type: ignore[arg-type]
            request_dir=request_dir,
            run_id=run_id,
            cache_key=payload.get("cache_key") if isinstance(payload.get("cache_key"), str) else None,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )

    for artifact in artifacts:
        add_entry_from_payload(artifact)

    for candidate in sorted(request_dir.rglob("*")):
        if candidate.is_file():
            add_entry_from_path(candidate)

    if tmp_dir.exists():
        for candidate in sorted(tmp_dir.rglob("*")):
            if not candidate.is_file():
                continue
            key = str(candidate.resolve())
            if key in entries:
                continue
            entries[key] = register_artifact(
                path=candidate.resolve(),
                resolved_path=candidate.resolve(),
                artifact_type="temp",
                purpose="temporary workspace artifact",
                format=candidate.suffix.lower().lstrip(".") or "binary",
                keep_policy="debug_only",
                include_in_export=False,
                storage="tmp",
                request_dir=request_dir,
                run_id=run_id,
            )

    manifest = {
        "run_id": run_id,
        "request_dir": str(request_dir),
        "artifacts": list(entries.values()),
    }
    if run_metadata:
        manifest.update(run_metadata)
    return manifest


def iter_artifacts_by_type(request_dir: Path, artifact_type: str) -> list[Path]:
    manifest = read_manifest(request_dir)
    if manifest is None:
        return []
    paths: list[Path] = []
    for artifact in manifest.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if artifact.get("artifact_type") != artifact_type:
            continue
        path = resolve_artifact_path(request_dir, artifact)
        if path.exists():
            paths.append(path)
    return sorted(paths)


def iter_exportable_artifacts(request_dir: Path) -> list[Path]:
    manifest = read_manifest(request_dir)
    if manifest is None:
        return []

    paths: list[Path] = []
    for artifact in manifest.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if artifact.get("artifact_type") not in {"final", "preview"}:
            continue
        if artifact.get("include_in_export") is not True:
            continue
        path = resolve_artifact_path(request_dir, artifact)
        if not path.exists() or not path.is_file():
            continue
        if path.name in {MANIFEST_FILENAME, "export_bundle.zip"}:
            continue
        if path.name in _WAYBACK_RGB_SOURCE_FILE_NAMES:
            continue
        if path.name.endswith("_valid_mask.tif"):
            continue
        paths.append(path)
    return sorted(paths)
