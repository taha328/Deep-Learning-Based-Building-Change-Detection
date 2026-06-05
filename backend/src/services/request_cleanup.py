from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Literal

from src.config import Settings


logger = logging.getLogger(__name__)

CleanupMode = Literal["off", "compact_heavy", "delete_full"]

HEAVY_FILE_NAMES = {
    "change_probability.tif",
    "building_change_mask.tif",
    "building_change_labels.tif",
    "building_change_polygons.geojsonl",
    "prediction_change_probability.tif",
    "prediction_change_mask.tif",
    "prediction_change_polygons.geojsonl",
    "change_probability_preview.png",
    "change_overlay_preview.png",
    "t1_preview.png",
    "t2_preview.png",
}
HEAVY_DIR_NAMES = {"tiles", "previews"}
PROVENANCE_FILE_NAMES = {
    "run_response.json",
    "manifest.json",
    "timing.json",
    "export_timing.json",
    "tiled_inference_metadata.json",
    "wayback_pair_summary.csv",
    "cleanup_report.json",
    "cleanup_audit.json",
}

REQUEST_TO_PROJECT_ARTIFACT_FILENAMES: dict[str, tuple[str, ...]] = {
    "building_change_polygons": ("additions.geojson", "automated_additions.geojson"),
    "building_change_blocks": ("effective_building_blocks.geojson", "automated_building_blocks.geojson"),
    "building_change_buffer_10m": ("building_change_buffer_10m.geojson",),
    "building_change_buffer_15m": ("building_change_buffer_15m.geojson",),
    "building_change_buffer_20m": ("building_change_buffer_20m.geojson",),
    "cumulative_union": ("cumulative_union.geojson",),
    "cumulative_convex_hull": ("cumulative_convex_hull.geojson",),
    "cumulative_concave_hull": ("cumulative_concave_hull.geojson",),
    "addition_candidate_diagnostics": ("addition_candidate_diagnostics.geojson",),
    "automated_additions": ("automated_additions.geojson",),
    "automated_building_blocks": ("automated_building_blocks.geojson",),
    "effective_building_blocks": ("effective_building_blocks.geojson",),
    "effective_footprint": ("effective_footprint.geojson",),
}
PUBLISHED_DUPLICATE_SUFFIXES = {".geojson", ".csv", ".geojsonl"}
REQUEST_REFERENCE_IMAGERY_FILENAMES = {"t1_wayback_rgb.tif": "t1", "t2_wayback_rgb.tif": "t2"}


@dataclass
class RequestDeletionAudit:
    request_hash: str
    pair_request_hash: str | None = None
    populated_request_hash: str | None = None
    request_workspace_path: str | None = None
    project_ids: list[str] = field(default_factory=list)
    status: str = "unknown"
    promotion_status: str = "unknown"
    frontend_dependencies: list[dict[str, Any]] = field(default_factory=list)
    qgis_dependencies: list[dict[str, Any]] = field(default_factory=list)
    backend_dependencies: list[dict[str, Any]] = field(default_factory=list)
    export_dependencies: list[dict[str, Any]] = field(default_factory=list)
    repair_dependencies: list[dict[str, Any]] = field(default_factory=list)
    artifact_paths_pointing_to_request: list[dict[str, Any]] = field(default_factory=list)
    reference_imagery_paths_pointing_to_request: list[dict[str, Any]] = field(default_factory=list)
    heavy_files: list[dict[str, Any]] = field(default_factory=list)
    provenance_files: list[dict[str, Any]] = field(default_factory=list)
    published_duplicate_files: list[dict[str, Any]] = field(default_factory=list)
    preserved_request_files: list[dict[str, Any]] = field(default_factory=list)
    skipped_published_duplicate_candidates: list[dict[str, Any]] = field(default_factory=list)
    safe_to_delete_full: bool = False
    safe_to_compact_heavy: bool = False
    recommended_policy: str = "off"
    blockers: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RequestCleanupReport:
    request_hash: str
    project_id: str | None
    release_identifier: str | None
    mode: str
    dry_run: bool
    audit: dict[str, Any]
    planned_deletions: list[dict[str, Any]]
    deleted: list[dict[str, Any]]
    preserved: list[dict[str, Any]]
    deleted_published_duplicates: list[dict[str, Any]] = field(default_factory=list)
    preserved_request_files: list[dict[str, Any]] = field(default_factory=list)
    skipped_published_duplicate_candidates: list[dict[str, Any]] = field(default_factory=list)
    pair_request_hash: str | None = None
    populated_request_hash: str | None = None
    request_workspace_path: str | None = None
    bytes_planned: int = 0
    bytes_deleted: int = 0
    cleanup_report_path: str | None = None
    cleanup_audit_path: str | None = None
    skipped: bool = False
    reason: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_size(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file() or child.is_symlink():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def _request_status(request_dir: Path) -> str:
    response = _read_json(request_dir / "run_response.json")
    if response is not None:
        success = response.get("success")
        if success is True:
            return "completed"
        if success is False:
            return "failed"
    manifest = _read_json(request_dir / "manifest.json")
    if manifest is not None and manifest.get("success") is True:
        return "completed"
    if any(request_dir.iterdir()) if request_dir.exists() else False:
        return "incomplete"
    return "unknown"


def _walk_values(value: Any, *, path: str = "") -> list[tuple[str, Any]]:
    found = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk_values(child, path=f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_values(child, path=f"{path}[{index}]"))
    return found


def _project_json_paths(settings: Settings, project_id: str | None = None) -> list[Path]:
    if project_id:
        return [settings.temporal_projects_dir / project_id / "project.json"]
    return sorted(settings.temporal_projects_dir.glob("*/project.json"))


def _artifact_paths_for_milestone(milestone: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for artifact in milestone.get("artifacts") or []:
        if isinstance(artifact, dict):
            for key in ("path", "url", "geojson_url", "qgis_url", "tilejson_url", "tiles_url_template"):
                value = artifact.get(key)
                if isinstance(value, str):
                    paths.append(value)
    return paths


def _is_promoted_milestone(milestone: dict[str, Any], request_hash: str, request_dir: Path) -> bool:
    if milestone.get("pair_request_hash") != request_hash and milestone.get("populated_request_hash") != request_hash:
        return False
    durable_artifacts = 0
    for raw in _artifact_paths_for_milestone(milestone):
        path = Path(raw)
        if path.is_absolute() and path.exists() and not _is_relative_to(path, request_dir):
            durable_artifacts += 1
    return durable_artifacts > 0


def _candidate_cleanup_targets(request_dir: Path, *, include_export_bundle: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    heavy: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    if not request_dir.exists():
        return heavy, provenance
    for child in sorted(request_dir.iterdir()):
        if child.name in HEAVY_DIR_NAMES and child.is_dir():
            heavy.append({"path": str(child), "kind": "directory", "size_bytes": _path_size(child), "reason": "heavy_request_tiles"})
            continue
        if child.name in HEAVY_FILE_NAMES and child.is_file():
            heavy.append({"path": str(child), "kind": "file", "size_bytes": _path_size(child), "reason": "heavy_intermediate"})
            continue
        if child.name == "export_bundle.zip" and include_export_bundle and child.is_file():
            heavy.append({"path": str(child), "kind": "file", "size_bytes": _path_size(child), "reason": "optional_regenerable_export_bundle"})
            continue
        if child.name in PROVENANCE_FILE_NAMES and child.is_file():
            provenance.append({"path": str(child), "kind": "file", "size_bytes": _path_size(child), "reason": "provenance"})
    return heavy, provenance


def _normalized_resolved(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.expanduser())


def _milestone_matches_request(milestone: dict[str, Any], request_hash: str, request_dir: Path) -> bool:
    if milestone.get("pair_request_hash") == request_hash or milestone.get("populated_request_hash") == request_hash:
        return True
    workspace = milestone.get("request_workspace_path")
    if isinstance(workspace, str) and workspace:
        return _normalized_resolved(Path(workspace)) == _normalized_resolved(request_dir)
    return False


def _matching_project_milestones(
    *,
    settings: Settings,
    request_hash: str,
    request_dir: Path,
    project_id: str | None,
) -> list[tuple[str, dict[str, Any], Path]]:
    matches: list[tuple[str, dict[str, Any], Path]] = []
    for project_json in _project_json_paths(settings, project_id):
        payload = _read_json(project_json)
        if payload is None:
            continue
        pid = str(payload.get("project_id") or payload.get("id") or project_json.parent.name)
        for milestone in payload.get("milestones") or []:
            if isinstance(milestone, dict) and _milestone_matches_request(milestone, request_hash, request_dir):
                matches.append((pid, milestone, project_json.parent))
    return matches


def _all_project_milestones(
    *,
    settings: Settings,
    project_id: str | None,
) -> list[tuple[str, dict[str, Any], Path]]:
    milestones: list[tuple[str, dict[str, Any], Path]] = []
    for project_json in _project_json_paths(settings, project_id):
        payload = _read_json(project_json)
        if payload is None:
            continue
        pid = str(payload.get("project_id") or payload.get("id") or project_json.parent.name)
        for milestone in payload.get("milestones") or []:
            if isinstance(milestone, dict):
                milestones.append((pid, milestone, project_json.parent))
    return milestones


def _project_artifact_file_for_candidate(
    *,
    request_path: Path,
    request_dir: Path,
    matched_milestones: list[tuple[str, dict[str, Any], Path]],
) -> tuple[Path | None, dict[str, Any] | None]:
    stem = request_path.stem
    expected_names = REQUEST_TO_PROJECT_ARTIFACT_FILENAMES.get(stem)
    if expected_names is None:
        return None, None
    for pid, milestone, project_dir in matched_milestones:
        release = milestone.get("release_identifier")
        if not isinstance(release, str) or not release:
            continue
        milestone_dir = project_dir / "milestones" / release
        artifact_paths = []
        for raw in _artifact_paths_for_milestone(milestone):
            path = Path(raw)
            if path.is_absolute():
                artifact_paths.append(path)
        for expected_name in expected_names:
            candidates = [milestone_dir / expected_name, *[path for path in artifact_paths if path.name == expected_name]]
            for candidate in candidates:
                if candidate.exists() and candidate.is_file() and not _is_relative_to(candidate, request_dir):
                    return candidate, {
                        "project_id": pid,
                        "release_identifier": release,
                        "published_path": str(candidate),
                        "published_filename": expected_name,
                    }
    return None, None


def _manifest_imagery_sources(request_dir: Path) -> dict[str, dict[str, Any]]:
    for path in (request_dir / "manifest.json", request_dir / "run_response.json"):
        payload = _read_json(path)
        if payload is None:
            continue
        candidates = [
            payload.get("imagery_sources"),
            ((payload.get("diagnostics") or {}).get("backend") or {}).get("imagery_sources")
            if isinstance(payload.get("diagnostics"), dict)
            else None,
        ]
        summary = payload.get("summary")
        if isinstance(summary, dict):
            candidates.append(summary.get("imagery_sources"))
        for candidate in candidates:
            if isinstance(candidate, dict):
                return {str(key): value for key, value in candidate.items() if isinstance(value, dict)}
    return {}


def _release_identifier_from_imagery_source(source: dict[str, Any]) -> str | None:
    for key in ("release_identifier", "releaseIdentifier", "release_id", "releaseId"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _project_reference_cog_for_request_image(
    *,
    request_path: Path,
    request_dir: Path,
    project_milestones: list[tuple[str, dict[str, Any], Path]],
    imagery_sources: dict[str, dict[str, Any]],
) -> tuple[Path | None, dict[str, Any] | None]:
    role = REQUEST_REFERENCE_IMAGERY_FILENAMES.get(request_path.name)
    if role is None:
        return None, None
    source = imagery_sources.get(role) or {}
    release_from_manifest = _release_identifier_from_imagery_source(source)
    if release_from_manifest is None:
        return None, {"reason": "missing_manifest_release_identifier", "role": role}
    if not source.get("canonical_cog_path") and not source.get("project_cog_path"):
        return None, {"reason": "missing_manifest_canonical_or_project_cog_proof", "role": role, "release_identifier": release_from_manifest}
    for pid, milestone, project_dir in project_milestones:
        release = milestone.get("release_identifier")
        if release != release_from_manifest:
            continue
        cog = project_dir / "milestones" / release_from_manifest / "reference_imagery_cog.tif"
        reference = milestone.get("reference_imagery") if isinstance(milestone.get("reference_imagery"), dict) else {}
        raw_cog = reference.get("cog_path") if isinstance(reference, dict) else None
        candidates = [cog]
        if isinstance(raw_cog, str) and raw_cog:
            candidates.append(Path(raw_cog))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0 and not _is_relative_to(candidate, request_dir):
                return candidate, {
                    "project_id": pid,
                    "release_identifier": release_from_manifest,
                    "published_path": str(candidate),
                    "role": role,
                    "manifest_canonical_cog_path": source.get("canonical_cog_path"),
                    "manifest_project_cog_path": source.get("project_cog_path"),
                }
    return None, {"reason": "missing_project_reference_cog", "role": role, "release_identifier": release_from_manifest}


def _published_duplicate_cleanup_targets(
    *,
    settings: Settings,
    request_hash: str,
    request_dir: Path,
    project_id: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    duplicates: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not request_dir.exists():
        return duplicates, preserved, skipped
    matched_milestones = _matching_project_milestones(
        settings=settings,
        request_hash=request_hash,
        request_dir=request_dir,
        project_id=project_id,
    )
    project_milestones = _all_project_milestones(settings=settings, project_id=project_id)
    imagery_sources = _manifest_imagery_sources(request_dir)
    for child in sorted(request_dir.iterdir()):
        if not child.is_file():
            continue
        if child.name in PROVENANCE_FILE_NAMES or child.name in HEAVY_FILE_NAMES or child.name == "export_bundle.zip":
            continue
        detail = {"path": str(child), "kind": "file", "size_bytes": _path_size(child)}
        if child.name in REQUEST_REFERENCE_IMAGERY_FILENAMES:
            target, proof = _project_reference_cog_for_request_image(
                request_path=child,
                request_dir=request_dir,
                project_milestones=project_milestones,
                imagery_sources=imagery_sources,
            )
            if target is not None and proof is not None:
                duplicates.append({**detail, "reason": "published_project_reference_imagery_duplicate", **proof})
            else:
                skipped.append({**detail, "reason": (proof or {}).get("reason") or "missing_reference_imagery_publication_proof", **(proof or {})})
            continue
        if child.suffix not in PUBLISHED_DUPLICATE_SUFFIXES:
            preserved.append({**detail, "reason": "request_file_not_published_duplicate_candidate"})
            continue
        target, proof = _project_artifact_file_for_candidate(
            request_path=child,
            request_dir=request_dir,
            matched_milestones=matched_milestones,
        )
        if target is not None and proof is not None:
            duplicates.append({**detail, "reason": "published_project_artifact_duplicate", **proof})
        else:
            skipped.append({**detail, "reason": "missing_project_artifact_publication_proof"})
    return duplicates, preserved, skipped


def audit_request_deletion_safety(
    *,
    request_hash: str,
    settings: Settings,
    project_id: str | None = None,
    pair_request_hash: str | None = None,
    populated_request_hash: str | None = None,
    request_workspace_path: str | None = None,
) -> RequestDeletionAudit:
    request_dir = settings.request_cache_dir / request_hash
    audit = RequestDeletionAudit(
        request_hash=request_hash,
        pair_request_hash=pair_request_hash,
        populated_request_hash=populated_request_hash or request_hash,
        request_workspace_path=request_workspace_path or str(request_dir),
    )
    if not request_dir.exists():
        audit.blockers.append("request_missing")
        return audit

    audit.status = _request_status(request_dir)
    heavy, provenance = _candidate_cleanup_targets(
        request_dir,
        include_export_bundle=settings.post_completion_request_cleanup_delete_export_bundle,
    )
    audit.heavy_files = heavy
    audit.provenance_files = provenance
    duplicates, preserved_request_files, skipped_duplicate_candidates = _published_duplicate_cleanup_targets(
        settings=settings,
        request_hash=request_hash,
        request_dir=request_dir,
        project_id=project_id,
    )
    audit.published_duplicate_files = duplicates
    audit.preserved_request_files = preserved_request_files
    audit.skipped_published_duplicate_candidates = skipped_duplicate_candidates
    promoted = False

    request_root_text = str(request_dir.resolve())
    for project_json in _project_json_paths(settings, project_id):
        payload = _read_json(project_json)
        if payload is None:
            continue
        pid = str(payload.get("project_id") or payload.get("id") or project_json.parent.name)
        project_mentions_request = False
        for field_path, value in _walk_values(payload):
            if isinstance(value, str) and value in {request_hash, pair_request_hash, populated_request_hash} and field_path.endswith(
                ("pair_request_hash", "populated_request_hash")
            ):
                project_mentions_request = True
                audit.backend_dependencies.append({"project_id": pid, "field": field_path, "reason": "request_hash_provenance"})
                audit.frontend_dependencies.append({"project_id": pid, "field": field_path, "reason": "cached_run_response_provenance"})
                audit.qgis_dependencies.append({"project_id": pid, "field": field_path, "reason": "project_provenance"})
            if isinstance(value, str) and request_root_text in value:
                project_mentions_request = True
                dependency = {"project_id": pid, "field": field_path, "path": value}
                if field_path.endswith("request_workspace_path"):
                    audit.backend_dependencies.append({**dependency, "reason": "request_workspace_provenance"})
                elif "reference_imagery" in field_path:
                    audit.reference_imagery_paths_pointing_to_request.append(dependency)
                elif "download_bundle" in field_path or "export_bundle" in value:
                    audit.export_dependencies.append(dependency)
                else:
                    audit.artifact_paths_pointing_to_request.append(dependency)
        for milestone in payload.get("milestones") or []:
            if isinstance(milestone, dict) and _is_promoted_milestone(milestone, request_hash, request_dir):
                promoted = True
        if project_mentions_request and pid not in audit.project_ids:
            audit.project_ids.append(pid)

    if audit.status != "completed":
        audit.blockers.append(f"request_status_{audit.status}")
    if audit.artifact_paths_pointing_to_request:
        audit.blockers.append("project_artifact_path_points_to_request")
    if audit.reference_imagery_paths_pointing_to_request:
        audit.blockers.append("reference_imagery_path_points_to_request")

    if promoted:
        audit.promotion_status = "promoted"
    elif audit.project_ids:
        audit.promotion_status = "partial"
        audit.blockers.append("promotion_not_proven")
    else:
        audit.promotion_status = "unknown"

    audit.safe_to_compact_heavy = (
        audit.status == "completed"
        and bool(audit.heavy_files or audit.published_duplicate_files)
        and not audit.artifact_paths_pointing_to_request
        and not audit.reference_imagery_paths_pointing_to_request
        and (promoted or not audit.project_ids)
    )
    full_delete_blockers = list(audit.blockers)
    if audit.project_ids:
        full_delete_blockers.append("request_still_referenced_by_temporal_project")
    if audit.export_dependencies:
        full_delete_blockers.append("export_bundle_or_download_path_points_to_request")
    if settings.post_completion_request_cleanup_mode != "delete_full":
        full_delete_blockers.append("delete_full_not_configured")
    if settings.post_completion_request_cleanup_keep_provenance:
        full_delete_blockers.append("provenance_keep_enabled")
    audit.safe_to_delete_full = audit.status == "completed" and not full_delete_blockers
    audit.recommended_policy = "compact_heavy" if audit.safe_to_compact_heavy else "off"
    if audit.safe_to_delete_full:
        audit.recommended_policy = "delete_full"
    for blocker in full_delete_blockers:
        if blocker not in audit.blockers and blocker.startswith(("request_", "export_", "delete_full", "provenance")):
            pass
    return audit


def _write_cleanup_report(request_dir: Path, report: RequestCleanupReport, *, filename: str = "cleanup_report.json") -> str | None:
    if not request_dir.exists():
        return None
    path = request_dir / filename
    path.write_text(json.dumps(report.model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def cleanup_request_after_successful_promotion(
    *,
    request_hash: str,
    project_id: str,
    release_identifier: str | None,
    mode: CleanupMode,
    settings: Settings,
    dry_run: bool = True,
    pair_request_hash: str | None = None,
    populated_request_hash: str | None = None,
    request_workspace_path: str | None = None,
) -> RequestCleanupReport:
    request_dir = settings.request_cache_dir / request_hash
    audit = audit_request_deletion_safety(
        request_hash=request_hash,
        settings=settings,
        project_id=project_id,
        pair_request_hash=pair_request_hash,
        populated_request_hash=populated_request_hash or request_hash,
        request_workspace_path=request_workspace_path or str(request_dir),
    )
    def _finish(report: RequestCleanupReport) -> RequestCleanupReport:
        if settings.post_completion_request_cleanup_keep_provenance and mode != "delete_full":
            audit_path = request_dir / "cleanup_audit.json"
            if report.skipped and audit_path.is_file():
                existing = _read_json(audit_path)
                if existing is not None and existing.get("skipped") is False:
                    report.cleanup_audit_path = str(audit_path)
                    return report
            report.cleanup_audit_path = _write_cleanup_report(request_dir, report, filename="cleanup_audit.json")
        return report

    if mode == "off":
        return _finish(RequestCleanupReport(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=mode,
            dry_run=dry_run,
            audit=audit.model_dump(),
            planned_deletions=[],
            deleted=[],
            preserved=audit.provenance_files,
            preserved_request_files=audit.preserved_request_files,
            skipped_published_duplicate_candidates=audit.skipped_published_duplicate_candidates,
            pair_request_hash=pair_request_hash,
            populated_request_hash=populated_request_hash or request_hash,
            request_workspace_path=request_workspace_path or str(request_dir),
            skipped=True,
            reason="mode_off",
        ))
    if mode == "compact_heavy" and not audit.safe_to_compact_heavy:
        return _finish(RequestCleanupReport(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=mode,
            dry_run=dry_run,
            audit=audit.model_dump(),
            planned_deletions=[],
            deleted=[],
            preserved=audit.provenance_files,
            preserved_request_files=audit.preserved_request_files,
            skipped_published_duplicate_candidates=audit.skipped_published_duplicate_candidates,
            pair_request_hash=pair_request_hash,
            populated_request_hash=populated_request_hash or request_hash,
            request_workspace_path=request_workspace_path or str(request_dir),
            skipped=True,
            reason="audit_not_safe_to_compact_heavy",
        ))
    if mode == "delete_full" and not audit.safe_to_delete_full:
        return _finish(RequestCleanupReport(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=mode,
            dry_run=dry_run,
            audit=audit.model_dump(),
            planned_deletions=[],
            deleted=[],
            preserved=audit.provenance_files,
            preserved_request_files=audit.preserved_request_files,
            skipped_published_duplicate_candidates=audit.skipped_published_duplicate_candidates,
            pair_request_hash=pair_request_hash,
            populated_request_hash=populated_request_hash or request_hash,
            request_workspace_path=request_workspace_path or str(request_dir),
            skipped=True,
            reason="audit_not_safe_to_delete_full",
        ))

    planned = (
        [*audit.heavy_files, *audit.published_duplicate_files]
        if mode == "compact_heavy"
        else [{"path": str(request_dir), "kind": "directory", "size_bytes": _path_size(request_dir), "reason": "safe_full_delete"}]
    )
    report = RequestCleanupReport(
        request_hash=request_hash,
        project_id=project_id,
        release_identifier=release_identifier,
        mode=mode,
        dry_run=dry_run,
        audit=audit.model_dump(),
        planned_deletions=planned,
        deleted=[],
        preserved=audit.provenance_files,
        preserved_request_files=audit.preserved_request_files,
        skipped_published_duplicate_candidates=audit.skipped_published_duplicate_candidates,
        pair_request_hash=pair_request_hash,
        populated_request_hash=populated_request_hash or request_hash,
        request_workspace_path=request_workspace_path or str(request_dir),
        bytes_planned=sum(int(item.get("size_bytes") or 0) for item in planned),
    )
    if dry_run:
        return _finish(report)

    for item in planned:
        path = Path(str(item["path"]))
        if not _is_relative_to(path, settings.request_cache_dir):
            raise ValueError(f"Refusing to delete path outside requests cache: {path}")
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        report.deleted.append(item)
        if str(item.get("reason") or "").startswith("published_project_"):
            report.deleted_published_duplicates.append(item)
        report.bytes_deleted += int(item.get("size_bytes") or 0)
    if settings.post_completion_request_cleanup_keep_provenance and mode != "delete_full":
        report.cleanup_report_path = _write_cleanup_report(request_dir, report)
    return _finish(report)


def run_post_completion_request_cleanup_if_enabled(
    *,
    request_hash: str,
    project_id: str,
    release_identifier: str | None,
    settings: Settings,
    pair_request_hash: str | None = None,
    populated_request_hash: str | None = None,
    request_workspace_path: str | None = None,
) -> RequestCleanupReport | None:
    physical_request_hash = populated_request_hash or request_hash
    physical_workspace_path = request_workspace_path or str(settings.request_cache_dir / physical_request_hash)
    if not settings.post_completion_request_cleanup_enabled or settings.post_completion_request_cleanup_mode == "off":
        logger.info(
            "POST_COMPLETION_REQUEST_CLEANUP_SKIPPED requestHash=%s pairRequestHash=%s populatedRequestHash=%s workspacePath=%s projectId=%s reason=disabled mode=%s",
            request_hash,
            pair_request_hash,
            physical_request_hash,
            physical_workspace_path,
            project_id,
            settings.post_completion_request_cleanup_mode,
        )
        return None
    logger.info(
        "POST_COMPLETION_REQUEST_CLEANUP_START requestHash=%s pairRequestHash=%s populatedRequestHash=%s workspacePath=%s projectId=%s releaseIdentifier=%s mode=%s",
        request_hash,
        pair_request_hash,
        physical_request_hash,
        physical_workspace_path,
        project_id,
        release_identifier,
        settings.post_completion_request_cleanup_mode,
    )
    try:
        report = cleanup_request_after_successful_promotion(
            request_hash=physical_request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=settings.post_completion_request_cleanup_mode,
            settings=settings,
            dry_run=False,
            pair_request_hash=pair_request_hash or request_hash,
            populated_request_hash=physical_request_hash,
            request_workspace_path=physical_workspace_path,
        )
        logger.info(
            "POST_COMPLETION_REQUEST_CLEANUP_DONE requestHash=%s pairRequestHash=%s populatedRequestHash=%s workspacePath=%s projectId=%s mode=%s skipped=%s bytesDeleted=%s reason=%s auditPath=%s",
            request_hash,
            pair_request_hash,
            physical_request_hash,
            physical_workspace_path,
            project_id,
            report.mode,
            report.skipped,
            report.bytes_deleted,
            report.reason,
            report.cleanup_audit_path,
        )
        return report
    except Exception as exc:
        logger.exception(
            "POST_COMPLETION_REQUEST_CLEANUP_ERROR requestHash=%s pairRequestHash=%s populatedRequestHash=%s workspacePath=%s projectId=%s error=%s",
            request_hash,
            pair_request_hash,
            physical_request_hash,
            physical_workspace_path,
            project_id,
            exc,
        )
        return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
