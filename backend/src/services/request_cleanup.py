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
}


@dataclass
class RequestDeletionAudit:
    request_hash: str
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
    bytes_planned: int = 0
    bytes_deleted: int = 0
    cleanup_report_path: str | None = None
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
    if milestone.get("pair_request_hash") != request_hash:
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


def audit_request_deletion_safety(
    *,
    request_hash: str,
    settings: Settings,
    project_id: str | None = None,
) -> RequestDeletionAudit:
    request_dir = settings.request_cache_dir / request_hash
    audit = RequestDeletionAudit(request_hash=request_hash)
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
    promoted = False

    request_root_text = str(request_dir.resolve())
    for project_json in _project_json_paths(settings, project_id):
        payload = _read_json(project_json)
        if payload is None:
            continue
        pid = str(payload.get("project_id") or payload.get("id") or project_json.parent.name)
        project_mentions_request = False
        for field_path, value in _walk_values(payload):
            if value == request_hash and field_path.endswith("pair_request_hash"):
                project_mentions_request = True
                audit.backend_dependencies.append({"project_id": pid, "field": field_path, "reason": "pair_request_hash_provenance"})
                audit.frontend_dependencies.append({"project_id": pid, "field": field_path, "reason": "cached_run_response_fallback"})
                audit.qgis_dependencies.append({"project_id": pid, "field": field_path, "reason": "project_provenance"})
            if isinstance(value, str) and request_root_text in value:
                project_mentions_request = True
                dependency = {"project_id": pid, "field": field_path, "path": value}
                if "reference_imagery" in field_path:
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
        and bool(audit.heavy_files)
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


def _write_cleanup_report(request_dir: Path, report: RequestCleanupReport) -> str | None:
    if not request_dir.exists():
        return None
    path = request_dir / "cleanup_report.json"
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
) -> RequestCleanupReport:
    request_dir = settings.request_cache_dir / request_hash
    audit = audit_request_deletion_safety(request_hash=request_hash, settings=settings, project_id=project_id)
    if mode == "off":
        return RequestCleanupReport(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=mode,
            dry_run=dry_run,
            audit=audit.model_dump(),
            planned_deletions=[],
            deleted=[],
            preserved=audit.provenance_files,
            skipped=True,
            reason="mode_off",
        )
    if mode == "compact_heavy" and not audit.safe_to_compact_heavy:
        return RequestCleanupReport(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=mode,
            dry_run=dry_run,
            audit=audit.model_dump(),
            planned_deletions=[],
            deleted=[],
            preserved=audit.provenance_files,
            skipped=True,
            reason="audit_not_safe_to_compact_heavy",
        )
    if mode == "delete_full" and not audit.safe_to_delete_full:
        return RequestCleanupReport(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=mode,
            dry_run=dry_run,
            audit=audit.model_dump(),
            planned_deletions=[],
            deleted=[],
            preserved=audit.provenance_files,
            skipped=True,
            reason="audit_not_safe_to_delete_full",
        )

    planned = audit.heavy_files if mode == "compact_heavy" else [{"path": str(request_dir), "kind": "directory", "size_bytes": _path_size(request_dir), "reason": "safe_full_delete"}]
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
        bytes_planned=sum(int(item.get("size_bytes") or 0) for item in planned),
    )
    if dry_run:
        return report

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
        report.bytes_deleted += int(item.get("size_bytes") or 0)
    if settings.post_completion_request_cleanup_keep_provenance and mode != "delete_full":
        report.cleanup_report_path = _write_cleanup_report(request_dir, report)
    return report


def run_post_completion_request_cleanup_if_enabled(
    *,
    request_hash: str,
    project_id: str,
    release_identifier: str | None,
    settings: Settings,
) -> RequestCleanupReport | None:
    if not settings.post_completion_request_cleanup_enabled or settings.post_completion_request_cleanup_mode == "off":
        logger.info(
            "POST_COMPLETION_REQUEST_CLEANUP_SKIPPED requestHash=%s projectId=%s reason=disabled mode=%s",
            request_hash,
            project_id,
            settings.post_completion_request_cleanup_mode,
        )
        return None
    logger.info(
        "POST_COMPLETION_REQUEST_CLEANUP_START requestHash=%s projectId=%s releaseIdentifier=%s mode=%s",
        request_hash,
        project_id,
        release_identifier,
        settings.post_completion_request_cleanup_mode,
    )
    try:
        report = cleanup_request_after_successful_promotion(
            request_hash=request_hash,
            project_id=project_id,
            release_identifier=release_identifier,
            mode=settings.post_completion_request_cleanup_mode,
            settings=settings,
            dry_run=False,
        )
        logger.info(
            "POST_COMPLETION_REQUEST_CLEANUP_DONE requestHash=%s projectId=%s mode=%s skipped=%s bytesDeleted=%s reason=%s",
            request_hash,
            project_id,
            report.mode,
            report.skipped,
            report.bytes_deleted,
            report.reason,
        )
        return report
    except Exception as exc:
        logger.exception(
            "POST_COMPLETION_REQUEST_CLEANUP_ERROR requestHash=%s projectId=%s error=%s",
            request_hash,
            project_id,
            exc,
        )
        return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
