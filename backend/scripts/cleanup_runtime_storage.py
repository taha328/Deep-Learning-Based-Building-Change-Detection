#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable


DEFAULT_MAX_ROWS = 50
DEFAULT_OLDER_THAN_HOURS = 72
DEFAULT_ACTIVE_WINDOW_HOURS = 24
DEFAULT_LARGE_JSON_THRESHOLD_BYTES = 50 * 1024 * 1024

REQUEST_FINAL_ARTIFACTS_PROTECTED_BY_DEFAULT = {
    "prediction_change_probability.tif",
    "prediction_change_mask.tif",
    "building_change_polygons.geojson",
    "building_change_polygons.geojsonl",
    "prediction_change_polygons.geojsonl",
    "run_response.json",
    "manifest.json",
    "timing.json",
}

REQUEST_ALLOWED_CHILD_TARGETS = {
    "export_bundle.zip": "orphan_export_bundle",
    "tiles": "orphan_tiles_directory",
    "tmp": "orphan_temporary_directory",
    "temp": "orphan_temporary_directory",
    "temporary": "orphan_temporary_directory",
    "intermediate": "orphan_intermediate_directory",
    "intermediates": "orphan_intermediate_directory",
    "staging": "orphan_staging_directory",
}

DERIVED_CACHE_DIRS = ("reference_tiles", "temporal_vector_tiles", "qgis_artifacts")
SCOPED_CACHE_DIRS = ("requests", "tmp", "dev_client_logs", "reference_tiles", "temporal_vector_tiles", "qgis_artifacts")
FORBIDDEN_CACHE_DIRS = (
    "wayback_mosaics",
    "imagery_cache",
    "temporal_projects",
    "db_payloads",
    "wayback_tile_cache",
    "wayback_tiles",
    "mapbox_mosaics",
    "wayback_metadata_cache",
    "wayback_tile_preflight_cache",
    "wayback_releases",
)

REQUIRED_MARKDOWN_SECTIONS = (
    "Mode",
    "Runtime Cache Location",
    "Summary",
    "Protected Requests",
    "Protected Request Reason Summary",
    "Protected Request Details",
    "False Protection Candidates",
    "Real Project Dependencies",
    "Pair Hash Only Requests",
    "Decoupling Actions",
    "Orphan Request Candidates",
    "Cleanup Candidates",
    "Forbidden / Protected Cache Areas",
    "Derived Cache Candidates",
    "Dev Log Candidates",
    "Skipped Unknown-Risk Items",
    "Estimated Bytes Reclaimable",
    "Actions Taken",
    "Errors",
    "Next Steps",
)


@dataclass(frozen=True)
class CleanupCandidate:
    path: str
    path_class: str
    size_bytes: int
    reason: str
    request_hash: str | None = None
    age_hours: float | None = None


@dataclass(frozen=True)
class RequestClassification:
    request_hash: str
    path: str
    request_dir: str
    size_bytes: int
    age_hours: float
    protected: bool
    orphan_candidate: bool
    reason: str
    status: str
    classification: str
    protection_reasons: list[dict[str, Any]]
    source_references: list[dict[str, Any]]
    project_ids: list[str]
    metadata_files: list[str]
    field_paths: list[str]
    referenced_paths: list[str]
    active_or_recent_reason: str | None = None
    unknown_risk_reason: str | None = None


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_runtime_cache_dir(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()
    env_path = os.environ.get("APP_RUNTIME_CACHE_DIR")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (repo_root_from_script() / "backend" / "runtime_cache").resolve()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def size_path(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() or item.is_symlink():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    if path.is_dir():
        for item in path.rglob("*"):
            try:
                latest = max(latest, item.stat().st_mtime)
            except OSError:
                continue
    return latest


def age_hours(path: Path, *, now: datetime) -> float:
    return max((now - datetime.fromtimestamp(latest_mtime(path), UTC)).total_seconds() / 3600.0, 0.0)


def safe_json_load(path: Path) -> tuple[Any | None, str | None]:
    try:
        if path.stat().st_size > DEFAULT_LARGE_JSON_THRESHOLD_BYTES:
            return None, "large_project_metadata_risk"
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # noqa: BLE001 - cleanup must report parse failures and continue.
        return None, f"json_parse_failed:{exc.__class__.__name__}"


def resolve_reference_path(value: str, *, project_dir: Path, runtime_cache_dir: Path) -> Path | None:
    if not value or value.startswith(("http://", "https://", "/api/")):
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    first = project_dir / path
    return first if first.exists() else runtime_cache_dir / path


def request_hash_from_path(path: Path, runtime_cache_dir: Path) -> str | None:
    try:
        rel = path.resolve().relative_to((runtime_cache_dir / "requests").resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def reason_for_path_reference(field_path: str, key: str) -> str:
    normalized = f"{field_path}.{key}".lower()
    if "download_bundle_path" in normalized or "downloadable_zip_path" in normalized or "temporal_project_bundle_path" in normalized:
        return "download_bundle_reference"
    if "reference_imagery" in normalized:
        return "reference_imagery_source_reference"
    if ".artifacts[" in normalized or ".artifacts." in normalized:
        return "artifact_path_reference"
    return "cached_response_reference"


def add_source_reference(
    refs_by_hash: dict[str, list[dict[str, Any]]],
    *,
    request_hash: str,
    reason: str,
    project_id: str,
    metadata_file: Path,
    field_path: str,
    value: Any,
    referenced_path: Path | None = None,
) -> None:
    refs_by_hash.setdefault(request_hash, []).append(
        {
            "reason": reason,
            "project_id": project_id,
            "metadata_file": str(metadata_file),
            "field_path": field_path,
            "value": str(value),
            "referenced_path": str(referenced_path) if referenced_path is not None else None,
        }
    )


def walk_json_for_references(
    value: Any,
    *,
    project_dir: Path,
    runtime_cache_dir: Path,
    request_references: dict[str, list[dict[str, Any]]],
    protected_paths: dict[str, list[dict[str, Any]]],
    project_id: str,
    metadata_file: Path,
    source: str,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_source = f"{source}.{key}" if source else str(key)
            if key in {"pair_request_hash", "pairRequestHash"} and isinstance(child, str) and child:
                add_source_reference(
                    request_references,
                    request_hash=child,
                    reason="pair_request_hash_reference",
                    project_id=project_id,
                    metadata_file=metadata_file,
                    field_path=child_source,
                    value=child,
                )
            if key in {
                "path",
                "url",
                "artifact_url",
                "artifactUrl",
                "download_url",
                "downloadUrl",
                "download_bundle_path",
                "downloadable_zip_path",
                "temporal_project_bundle_path",
                "cog_path",
                "cogPath",
                "canonical_cog_path",
                "canonicalCogPath",
                "source_path",
                "sourcePath",
                "display_path",
                "displayPath",
            } and isinstance(child, str):
                ref_path = resolve_reference_path(child, project_dir=project_dir, runtime_cache_dir=runtime_cache_dir)
                if ref_path is not None:
                    request_hash = request_hash_from_path(ref_path, runtime_cache_dir)
                    reason = reason_for_path_reference(child_source.rsplit(".", 1)[0] if "." in child_source else child_source, key)
                    ref = {
                        "reason": reason,
                        "project_id": project_id,
                        "metadata_file": str(metadata_file),
                        "field_path": child_source,
                        "value": child,
                        "referenced_path": str(ref_path),
                    }
                    protected_paths.setdefault(str(ref_path), []).append(ref)
                    if request_hash:
                        request_references.setdefault(request_hash, []).append(ref)
            elif isinstance(child, str) and "/requests/" in child:
                ref_path = resolve_reference_path(child, project_dir=project_dir, runtime_cache_dir=runtime_cache_dir)
                if ref_path is not None:
                    reason = reason_for_path_reference(child_source, "")
                    ref = {
                        "reason": reason,
                        "project_id": project_id,
                        "metadata_file": str(metadata_file),
                        "field_path": child_source,
                        "value": child,
                        "referenced_path": str(ref_path),
                    }
                    protected_paths.setdefault(str(ref_path), []).append(ref)
                    request_hash = request_hash_from_path(ref_path, runtime_cache_dir)
                    if request_hash:
                        request_references.setdefault(request_hash, []).append(ref)
            walk_json_for_references(
                child,
                project_dir=project_dir,
                runtime_cache_dir=runtime_cache_dir,
                request_references=request_references,
                protected_paths=protected_paths,
                project_id=project_id,
                metadata_file=metadata_file,
                source=child_source,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk_json_for_references(
                child,
                project_dir=project_dir,
                runtime_cache_dir=runtime_cache_dir,
                request_references=request_references,
                protected_paths=protected_paths,
                project_id=project_id,
                metadata_file=metadata_file,
                source=f"{source}[{index}]",
            )


def normalize_project_id(project_dir: Path, payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("project_id") or payload.get("projectId") or payload.get("id")
        if value:
            return str(value)
    return project_dir.name


def collect_project_references(runtime_cache_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    request_references: dict[str, list[dict[str, Any]]] = {}
    protected_paths: dict[str, list[dict[str, Any]]] = {}
    unknown_risk_items: list[dict[str, Any]] = []
    temporal_root = runtime_cache_dir / "temporal_projects"
    if not temporal_root.is_dir():
        return request_references, protected_paths, unknown_risk_items
    for project_dir in sorted(path for path in temporal_root.iterdir() if path.is_dir()):
        for relative in (
            "project.json",
            "project_manifest.json",
            "project_summary.json",
            "reference_layers/reference_layers.json",
        ):
            path = project_dir / relative
            if not path.is_file():
                continue
            payload, reason = safe_json_load(path)
            if reason is not None:
                unknown_risk_items.append(
                    {
                        "path": str(path),
                        "reason": "large_or_unparseable_project_metadata" if reason == "large_project_metadata_risk" else "unknown_risk_metadata",
                        "detail": reason,
                        "project_id": project_dir.name,
                        "metadata_file": str(path),
                        "size_bytes": size_path(path),
                        "cleanup_impact": "reported_only; does_not_protect_all_requests",
                    }
                )
                continue
            project_id = normalize_project_id(project_dir, payload)
            walk_json_for_references(
                payload,
                project_dir=project_dir,
                runtime_cache_dir=runtime_cache_dir,
                request_references=request_references,
                protected_paths=protected_paths,
                project_id=project_id,
                metadata_file=path,
                source=relative,
            )
    return request_references, protected_paths, unknown_risk_items


def has_active_marker(request_dir: Path) -> bool:
    marker_names = {
        ".lock",
        "lock",
        "status.json",
        "progress.json",
        "job_progress.json",
        "incomplete",
        "running",
        "staging",
    }
    return any(path.name in marker_names or path.suffix == ".lock" for path in request_dir.rglob("*"))


def classify_requests(
    runtime_cache_dir: Path,
    *,
    request_references: dict[str, list[dict[str, Any]]],
    protected_paths: dict[str, list[dict[str, Any]]],
    older_than_hours: int,
    active_window_hours: int,
    now: datetime,
) -> tuple[list[RequestClassification], list[RequestClassification], list[dict[str, Any]]]:
    request_root = runtime_cache_dir / "requests"
    protected: list[RequestClassification] = []
    orphan_candidates: list[RequestClassification] = []
    unknown_risk: list[dict[str, Any]] = []
    if not request_root.is_dir():
        return protected, orphan_candidates, unknown_risk

    protected_path_objects = [Path(value) for value in protected_paths]
    for request_dir in sorted(path for path in request_root.iterdir() if path.is_dir()):
        request_hash = request_dir.name
        request_size = size_path(request_dir)
        request_age = age_hours(request_dir, now=now)
        path_refs: list[dict[str, Any]] = []
        for ref in protected_path_objects:
            if request_dir.resolve() == ref.resolve() or request_dir.resolve() in ref.resolve().parents:
                path_refs.extend(protected_paths.get(str(ref), []))
        hash_refs = request_references.get(request_hash, [])
        pair_refs = [ref for ref in hash_refs if ref.get("reason") == "pair_request_hash_reference"]
        dependency_refs = [ref for ref in hash_refs if ref.get("reason") != "pair_request_hash_reference"]
        for ref in path_refs:
            if ref not in dependency_refs and ref.get("reason") != "pair_request_hash_reference":
                dependency_refs.append(ref)
        recent_or_active = request_age < active_window_hours
        complete = (request_dir / "run_response.json").is_file() or (request_dir / "manifest.json").is_file()
        active_marker = has_active_marker(request_dir)
        final_artifacts = [name for name in REQUEST_FINAL_ARTIFACTS_PROTECTED_BY_DEFAULT if (request_dir / name).exists()]

        reason_parts: list[str] = []
        protection_reasons: list[dict[str, Any]] = []
        is_protected = False
        is_unknown = False
        if dependency_refs:
            is_protected = True
            for ref in dependency_refs:
                reason = str(ref.get("reason") or "cached_response_reference")
                if reason not in reason_parts:
                    reason_parts.append(reason)
                protection_reasons.append(ref)
        if recent_or_active:
            is_protected = True
            reason_parts.append("recent_or_active_request")
            protection_reasons.append(
                {
                    "reason": "recent_or_active_request",
                    "project_id": None,
                    "metadata_file": None,
                    "field_path": None,
                    "value": f"age_hours={round(request_age, 2)} active_window_hours={active_window_hours}",
                    "referenced_path": str(request_dir),
                }
            )
        if active_marker:
            is_protected = True
            reason_parts.append("recent_or_active_request")
            protection_reasons.append(
                {
                    "reason": "recent_or_active_request",
                    "project_id": None,
                    "metadata_file": None,
                    "field_path": None,
                    "value": "active_status_or_lock_marker",
                    "referenced_path": str(request_dir),
                }
            )
        if not complete:
            is_protected = True
            is_unknown = True
            reason_parts.append("incomplete_request")
            protection_reasons.append(
                {
                    "reason": "incomplete_request",
                    "project_id": None,
                    "metadata_file": None,
                    "field_path": None,
                    "value": "missing_run_response_or_manifest",
                    "referenced_path": str(request_dir),
                }
            )
        if final_artifacts:
            reason_parts.append("contains_protected_final_artifacts")

        old_enough = request_age >= older_than_hours
        if is_protected or not old_enough:
            if not reason_parts and not old_enough:
                reason_parts.append("recent_or_active_request")
                protection_reasons.append(
                    {
                        "reason": "recent_or_active_request",
                        "project_id": None,
                        "metadata_file": None,
                        "field_path": None,
                        "value": f"younger_than_ttl age_hours={round(request_age, 2)} older_than_hours={older_than_hours}",
                        "referenced_path": str(request_dir),
                    }
                )
            source_refs = dependency_refs + pair_refs
            classification = RequestClassification(
                request_hash=request_hash,
                path=str(request_dir),
                request_dir=str(request_dir),
                size_bytes=request_size,
                age_hours=round(request_age, 2),
                protected=True,
                orphan_candidate=False,
                reason=";".join(reason_parts),
                status="protected",
                classification="protected_request",
                protection_reasons=protection_reasons,
                source_references=source_refs,
                project_ids=sorted({str(ref["project_id"]) for ref in source_refs if ref.get("project_id")}),
                metadata_files=sorted({str(ref["metadata_file"]) for ref in source_refs if ref.get("metadata_file")}),
                field_paths=sorted({str(ref["field_path"]) for ref in source_refs if ref.get("field_path")}),
                referenced_paths=sorted({str(ref["referenced_path"]) for ref in source_refs if ref.get("referenced_path")}),
                active_or_recent_reason=";".join(
                    str(ref.get("value")) for ref in protection_reasons if ref.get("reason") == "recent_or_active_request"
                )
                or None,
                unknown_risk_reason="incomplete_request" if is_unknown else None,
            )
            protected.append(classification)
            if is_unknown:
                unknown_risk.append(
                    {
                        "path": str(request_dir),
                        "reason": classification.reason,
                        "size_bytes": request_size,
                    }
                )
            continue

        orphan_candidates.append(
            RequestClassification(
                request_hash=request_hash,
                path=str(request_dir),
                request_dir=str(request_dir),
                size_bytes=request_size,
                age_hours=round(request_age, 2),
                protected=False,
                orphan_candidate=True,
                reason="old_unreferenced_completed_request"
                if not pair_refs
                else "old_pair_hash_only_completed_request;pair_hash_is_provenance_not_storage_dependency",
                status="orphan_candidate",
                classification="pair_hash_only_orphan_candidate" if pair_refs else "orphan_candidate",
                protection_reasons=[],
                source_references=pair_refs,
                project_ids=sorted({str(ref["project_id"]) for ref in pair_refs if ref.get("project_id")}),
                metadata_files=sorted({str(ref["metadata_file"]) for ref in pair_refs if ref.get("metadata_file")}),
                field_paths=sorted({str(ref["field_path"]) for ref in pair_refs if ref.get("field_path")}),
                referenced_paths=[],
            )
        )
    return protected, orphan_candidates, unknown_risk


def collect_request_cleanup_candidates(
    orphan_requests: Iterable[RequestClassification],
    *,
    older_than_hours: int,
    now: datetime,
) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for request in orphan_requests:
        request_dir = Path(request.path)
        for name, path_class in REQUEST_ALLOWED_CHILD_TARGETS.items():
            path = request_dir / name
            if not path.exists():
                continue
            path_age = age_hours(path, now=now)
            if path_age < older_than_hours:
                continue
            candidates.append(
                CleanupCandidate(
                    path=str(path),
                    path_class=path_class,
                    size_bytes=size_path(path),
                    reason=f"{path_class} in old orphan request",
                    request_hash=request.request_hash,
                    age_hours=round(path_age, 2),
                )
            )
    return candidates


def collect_stale_tmp_candidates(runtime_cache_dir: Path, *, older_than_hours: int, now: datetime) -> list[CleanupCandidate]:
    tmp_root = runtime_cache_dir / "tmp"
    candidates: list[CleanupCandidate] = []
    if not tmp_root.is_dir():
        return candidates
    for child in sorted(tmp_root.iterdir()):
        path_age = age_hours(child, now=now)
        if path_age >= older_than_hours:
            candidates.append(
                CleanupCandidate(
                    path=str(child),
                    path_class="stale_tmp_entry",
                    size_bytes=size_path(child),
                    reason="stale runtime_cache/tmp entry older than TTL",
                    age_hours=round(path_age, 2),
                )
            )
    return candidates


def collect_derived_cache_candidates(runtime_cache_dir: Path, *, older_than_hours: int, now: datetime) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for folder in DERIVED_CACHE_DIRS:
        root = runtime_cache_dir / folder
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            path_age = age_hours(child, now=now)
            if path_age >= older_than_hours:
                candidates.append(
                    CleanupCandidate(
                        path=str(child),
                        path_class=f"derived_{folder}_entry",
                        size_bytes=size_path(child),
                        reason=f"derived rebuildable cache entry under {folder}",
                        age_hours=round(path_age, 2),
                    )
                )
    return candidates


def collect_dev_log_candidates(runtime_cache_dir: Path, *, max_rows: int) -> list[dict[str, Any]]:
    root = runtime_cache_dir / "dev_client_logs"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append(
            {
                "path": str(path),
                "size_bytes": size_path(path),
                "reason": "reported_only_by_default; use future explicit dev-log cleanup flag",
            }
        )
    rows.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
    return rows[:max_rows]


def forbidden_cache_areas(runtime_cache_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "path": str(runtime_cache_dir / name),
            "exists": (runtime_cache_dir / name).exists(),
            "reason": "forbidden_in_phase_2",
        }
        for name in FORBIDDEN_CACHE_DIRS
    ]


def candidate_from_dict(candidate: CleanupCandidate | dict[str, Any]) -> CleanupCandidate:
    if isinstance(candidate, CleanupCandidate):
        return candidate
    return CleanupCandidate(**candidate)


def is_under_forbidden_area(path: Path, runtime_cache_dir: Path) -> bool:
    return any(is_relative_to(path, runtime_cache_dir / name) for name in FORBIDDEN_CACHE_DIRS)


def verify_delete_candidate(path: Path, candidate: CleanupCandidate, runtime_cache_dir: Path, protected_paths: dict[str, list[str]]) -> str | None:
    resolved_runtime = runtime_cache_dir.resolve()
    try:
        resolved_path = path.resolve()
    except OSError as exc:
        return f"resolve_failed:{exc.__class__.__name__}"
    if resolved_path == resolved_runtime:
        return "refuse_runtime_cache_root"
    if not is_relative_to(resolved_path, resolved_runtime):
        return "path_outside_runtime_cache"
    if is_under_forbidden_area(resolved_path, resolved_runtime):
        return "path_inside_forbidden_cache_area"
    if str(path) in protected_paths or str(resolved_path) in protected_paths:
        return "path_is_protected_reference"
    if candidate.path_class not in {
        "orphan_export_bundle",
        "orphan_tiles_directory",
        "orphan_temporary_directory",
        "orphan_intermediate_directory",
        "orphan_staging_directory",
        "stale_tmp_entry",
        "derived_reference_tiles_entry",
        "derived_temporal_vector_tiles_entry",
        "derived_qgis_artifacts_entry",
    }:
        return "unsupported_candidate_class"
    return None


def apply_cleanup_candidates(
    candidates: Iterable[CleanupCandidate],
    *,
    runtime_cache_dir: Path,
    protected_paths: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        path = Path(candidate.path)
        error = verify_delete_candidate(path, candidate, runtime_cache_dir, protected_paths)
        if error:
            errors.append({"path": str(path), "error": error, "candidate": asdict(candidate)})
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
            actions.append({"path": str(path), "action": "deleted", "size_bytes": candidate.size_bytes, "path_class": candidate.path_class})
        except Exception as exc:  # noqa: BLE001 - report per-path cleanup failures without continuing blindly.
            errors.append({"path": str(path), "error": f"delete_failed:{exc.__class__.__name__}:{exc}", "candidate": asdict(candidate)})
    return actions, errors


def build_report(
    runtime_cache_dir: Path,
    *,
    apply: bool,
    yes: bool,
    older_than_hours: int,
    active_window_hours: int,
    max_rows: int,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    request_references, protected_paths, unknown_risk_items = collect_project_references(runtime_cache_dir)
    protected_requests, orphan_requests, request_unknown_risk = classify_requests(
        runtime_cache_dir,
        request_references=request_references,
        protected_paths=protected_paths,
        older_than_hours=older_than_hours,
        active_window_hours=active_window_hours,
        now=now,
    )
    cleanup_candidates = (
        collect_request_cleanup_candidates(orphan_requests, older_than_hours=older_than_hours, now=now)
        + collect_stale_tmp_candidates(runtime_cache_dir, older_than_hours=older_than_hours, now=now)
        + collect_derived_cache_candidates(runtime_cache_dir, older_than_hours=older_than_hours, now=now)
    )
    dev_log_candidates = collect_dev_log_candidates(runtime_cache_dir, max_rows=max_rows)
    actions_taken: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    mode = "apply" if apply else "dry_run"
    if apply:
        if not yes:
            errors.append({"error": "apply_requires_yes", "message": "--apply requires --yes; no files were deleted"})
        else:
            actions_taken, errors = apply_cleanup_candidates(
                cleanup_candidates,
                runtime_cache_dir=runtime_cache_dir,
                protected_paths=protected_paths,
            )
    estimated_bytes_reclaimable = sum(candidate.size_bytes for candidate in cleanup_candidates)
    protection_reason_counts: dict[str, int] = {}
    for request in protected_requests:
        for ref in request.protection_reasons:
            reason = str(ref.get("reason") or "unknown_risk_metadata")
            protection_reason_counts[reason] = protection_reason_counts.get(reason, 0) + 1
    pair_hash_only_requests = [request for request in orphan_requests if request.classification == "pair_hash_only_orphan_candidate"]
    false_protection_candidates = [
        asdict(request)
        for request in pair_hash_only_requests[:max_rows]
    ]
    real_project_dependencies = [
        asdict(request)
        for request in protected_requests
        if any(ref.get("reason") in {"artifact_path_reference", "download_bundle_reference", "reference_imagery_source_reference", "cached_response_reference"} for ref in request.protection_reasons)
    ][:max_rows]
    report = {
        "mode": mode,
        "runtime_cache_dir": str(runtime_cache_dir),
        "summary": {
            "runtime_cache_exists": runtime_cache_dir.exists(),
            "scoped_cache_dirs": list(SCOPED_CACHE_DIRS),
            "protected_request_count": len(protected_requests),
            "orphan_request_candidate_count": len(orphan_requests),
            "cleanup_candidate_count": len(cleanup_candidates),
            "derived_cache_candidate_count": len([item for item in cleanup_candidates if item.path_class.startswith("derived_")]),
            "dev_log_candidate_count": len(dev_log_candidates),
            "unknown_risk_count": len(unknown_risk_items) + len(request_unknown_risk),
            "pair_hash_only_request_count": len(pair_hash_only_requests),
            "real_project_dependency_request_count": len(real_project_dependencies),
        },
        "protection_reason_counts": {
            "pair_request_hash_reference": sum(
                1
                for refs in request_references.values()
                for ref in refs
                if ref.get("reason") == "pair_request_hash_reference"
            ),
            "artifact_path_reference": protection_reason_counts.get("artifact_path_reference", 0),
            "download_bundle_reference": protection_reason_counts.get("download_bundle_reference", 0),
            "reference_imagery_source_reference": protection_reason_counts.get("reference_imagery_source_reference", 0),
            "cached_response_reference": protection_reason_counts.get("cached_response_reference", 0),
            "recent_or_active_request": protection_reason_counts.get("recent_or_active_request", 0),
            "incomplete_request": protection_reason_counts.get("incomplete_request", 0),
            "large_or_unparseable_project_metadata": len([item for item in unknown_risk_items if item.get("reason") == "large_or_unparseable_project_metadata"]),
            "unknown_risk_metadata": len([item for item in unknown_risk_items if item.get("reason") == "unknown_risk_metadata"]),
        },
        "protected_requests": [asdict(item) for item in protected_requests[:max_rows]],
        "orphan_request_candidates": [asdict(item) for item in orphan_requests[:max_rows]],
        "pair_hash_only_requests": [asdict(item) for item in pair_hash_only_requests[:max_rows]],
        "false_protection_candidates": false_protection_candidates,
        "real_project_dependencies": real_project_dependencies,
        "cleanup_candidates": [asdict(item) for item in cleanup_candidates[:max_rows]],
        "forbidden_cache_areas": forbidden_cache_areas(runtime_cache_dir),
        "derived_cache_candidates": [asdict(item) for item in cleanup_candidates if item.path_class.startswith("derived_")][:max_rows],
        "dev_log_candidates": dev_log_candidates,
        "unknown_risk_items": (unknown_risk_items + request_unknown_risk)[:max_rows],
        "estimated_bytes_reclaimable": estimated_bytes_reclaimable,
        "actions_taken": actions_taken,
        "errors": errors,
        "next_steps": [
            "Review dry-run candidates before applying.",
            "Run apply only with --apply --yes.",
            "Pair-request-hash-only requests are provenance-only and may be cleaned after TTL when no durable artifact path points into the request.",
            "Keep wayback_mosaics, imagery_cache, temporal_projects, and db_payloads out of Phase 2 cleanup.",
            "Add explicit dev log cleanup flag before truncating development logs.",
        ],
    }
    return report


def human_size(size_bytes: int | float) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def md_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], *, max_rows: int) -> str:
    if not rows:
        return "_None._"
    lines = ["| " + " | ".join(title for title, _key in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows[:max_rows]:
        values: list[str] = []
        for _title, key in columns:
            value = row.get(key)
            if key.endswith("size_bytes") or key == "estimated_bytes_reclaimable":
                value = human_size(int(value or 0))
            elif isinstance(value, bool):
                value = "yes" if value else "no"
            values.append(str(value if value is not None else ""))
        lines.append("| " + " | ".join(" ".join(value.splitlines()) for value in values) + " |")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any], *, max_rows: int) -> str:
    summary = report["summary"]
    sections = ["# Runtime Storage Cleanup Report — Phase 2"]
    sections.append(f"## Mode\n\n`{report['mode']}`")
    sections.append(f"## Runtime Cache Location\n\n`{report['runtime_cache_dir']}`")
    sections.append(
        "\n".join(
            [
                "## Summary",
                "",
                f"- Protected requests: {summary['protected_request_count']}",
                f"- Orphan request candidates: {summary['orphan_request_candidate_count']}",
                f"- Cleanup candidates: {summary['cleanup_candidate_count']}",
                f"- Derived cache candidates: {summary['derived_cache_candidate_count']}",
                f"- Dev log candidates reported: {summary['dev_log_candidate_count']}",
                f"- Unknown-risk items: {summary['unknown_risk_count']}",
                f"- Pair-hash-only requests: {summary['pair_hash_only_request_count']}",
                f"- Real project dependency requests: {summary['real_project_dependency_request_count']}",
            ]
        )
    )
    sections.append(
        "## Protected Request Reason Summary\n\n"
        + md_table(
            [{"reason": key, "count": value} for key, value in sorted(report["protection_reason_counts"].items())],
            [("Reason", "reason"), ("Count", "count")],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Protected Requests\n\n"
        + md_table(
            report["protected_requests"],
            [("Request", "request_hash"), ("Size", "size_bytes"), ("Age hours", "age_hours"), ("Reason", "reason"), ("Projects", "project_ids")],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Protected Request Details\n\n"
        + md_table(
            report["protected_requests"],
            [("Request", "request_hash"), ("Metadata files", "metadata_files"), ("Field paths", "field_paths"), ("Referenced paths", "referenced_paths")],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## False Protection Candidates\n\n"
        + md_table(
            report["false_protection_candidates"],
            [("Request", "request_hash"), ("Size", "size_bytes"), ("Reason", "reason"), ("Pair refs", "field_paths")],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Real Project Dependencies\n\n"
        + md_table(
            report["real_project_dependencies"],
            [("Request", "request_hash"), ("Size", "size_bytes"), ("Reasons", "reason"), ("Referenced paths", "referenced_paths")],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Pair Hash Only Requests\n\n"
        + md_table(
            report["pair_hash_only_requests"],
            [("Request", "request_hash"), ("Size", "size_bytes"), ("Age hours", "age_hours"), ("Reason", "reason")],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Decoupling Actions\n\n"
        "_None. No request artifact path decoupling was required by this cleanup pass; pair_request_hash values are provenance only._"
    )
    sections.append(
        "## Orphan Request Candidates\n\n"
        + md_table(report["orphan_request_candidates"], [("Request", "request_hash"), ("Size", "size_bytes"), ("Age hours", "age_hours"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(
        "## Cleanup Candidates\n\n"
        + md_table(report["cleanup_candidates"], [("Class", "path_class"), ("Path", "path"), ("Size", "size_bytes"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(
        "## Forbidden / Protected Cache Areas\n\n"
        + md_table(report["forbidden_cache_areas"], [("Name", "name"), ("Exists", "exists"), ("Path", "path"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(
        "## Derived Cache Candidates\n\n"
        + md_table(report["derived_cache_candidates"], [("Class", "path_class"), ("Path", "path"), ("Size", "size_bytes"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(
        "## Dev Log Candidates\n\n"
        + md_table(report["dev_log_candidates"], [("Path", "path"), ("Size", "size_bytes"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(
        "## Skipped Unknown-Risk Items\n\n"
        + md_table(report["unknown_risk_items"], [("Path", "path"), ("Size", "size_bytes"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(f"## Estimated Bytes Reclaimable\n\n{human_size(report['estimated_bytes_reclaimable'])}")
    sections.append(
        "## Actions Taken\n\n"
        + md_table(report["actions_taken"], [("Action", "action"), ("Path", "path"), ("Size", "size_bytes"), ("Class", "path_class")], max_rows=max_rows)
    )
    sections.append(
        "## Errors\n\n"
        + md_table(report["errors"], [("Path", "path"), ("Error", "error"), ("Message", "message")], max_rows=max_rows)
    )
    sections.append("## Next Steps\n\n" + "\n".join(f"- {item}" for item in report["next_steps"]))
    return "\n\n".join(sections) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2 safe runtime storage cleanup for request workspaces and derived caches.")
    parser.add_argument("--runtime-cache-dir", help="Runtime cache directory. Defaults to APP_RUNTIME_CACHE_DIR or backend/runtime_cache.")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run mode. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Delete approved candidates. Requires --yes.")
    parser.add_argument("--yes", action="store_true", help="Confirm apply mode deletion.")
    parser.add_argument("--older-than-hours", type=int, default=DEFAULT_OLDER_THAN_HOURS)
    parser.add_argument("--active-window-hours", type=int, default=DEFAULT_ACTIVE_WINDOW_HOURS)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime_cache_dir = resolve_runtime_cache_dir(args.runtime_cache_dir)
    if not runtime_cache_dir.is_dir():
        message = f"Runtime cache directory not found: {runtime_cache_dir}"
        if args.json:
            print(json.dumps({"error": message, "runtime_cache_dir": str(runtime_cache_dir)}, indent=2))
        else:
            print(f"# Runtime Storage Cleanup Report — Phase 2\n\n{message}", file=sys.stderr)
        return 2
    report = build_report(
        runtime_cache_dir,
        apply=bool(args.apply),
        yes=bool(args.yes),
        older_than_hours=max(args.older_than_hours, 1),
        active_window_hours=max(args.active_window_hours, 1),
        max_rows=max(args.max_rows, 1),
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report, max_rows=max(args.max_rows, 1)))
    if args.apply and not args.yes:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
