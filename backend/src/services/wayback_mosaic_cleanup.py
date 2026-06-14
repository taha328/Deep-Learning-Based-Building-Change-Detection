from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.config import Settings
from src.domain.reference_imagery_cache import read_reference_imagery_cache_metadata
from src.schemas import TemporalProject


logger = logging.getLogger(__name__)

COMPACTABLE_FILES = ("mosaic.tif", "mosaic.png")
REFERENCE_SCAN_CHUNK_BYTES = 1024 * 1024


def _size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _contains_active_reference(*, runtime_cache_root: Path, cache_key: str) -> bool:
    needles = tuple(f"wayback_mosaics/{cache_key}/{name}" for name in COMPACTABLE_FILES)
    overlap = max(len(needle) for needle in needles) - 1
    for reference_root in (runtime_cache_root / "requests", runtime_cache_root / "temporal_projects"):
        if not reference_root.is_dir():
            continue
        for path in reference_root.rglob("*.json"):
            if any(part.lower() in {"backup", "backups", ".backup", "metadata_backups"} for part in path.parts):
                continue
            try:
                carry = ""
                with path.open("rb") as handle:
                    while chunk := handle.read(REFERENCE_SCAN_CHUNK_BYTES):
                        payload = (carry + chunk.decode("utf-8", errors="ignore")).replace("\\", "/")
                        if any(needle in payload for needle in needles):
                            return True
                        carry = payload[-overlap:]
            except (OSError, UnicodeError):
                # Unreadable request/project metadata is unknown risk, so cleanup must be conservative.
                return True
    return False


def _log_skipped(
    *,
    project_id: str,
    release_identifier: str,
    reason: str,
    wayback_dir: Path,
    final_reference_path: Path,
) -> None:
    logger.warning(
        "WAYBACK_MOSAIC_CACHE_CLEANUP_SKIPPED projectId=%s releaseIdentifier=%s reason=%s waybackDir=%s finalReferencePath=%s",
        project_id,
        release_identifier,
        reason,
        wayback_dir,
        final_reference_path,
    )


def cleanup_wayback_mosaic_cache_after_success(
    *,
    project_id: str,
    release_identifier: str,
    wayback_mosaic_dir: Path,
    final_reference_path: Path,
    wayback_mosaics_root: Path,
    runtime_cache_root: Path,
) -> dict[str, Any]:
    """Compact one derived Wayback mosaic after final project imagery exists."""

    resolved_root = wayback_mosaics_root.expanduser().resolve()
    resolved_target = wayback_mosaic_dir.expanduser().resolve()
    resolved_final = final_reference_path.expanduser().resolve()

    if not resolved_final.is_file():
        _log_skipped(
            project_id=project_id,
            release_identifier=release_identifier,
            reason="FINAL_REFERENCE_MISSING",
            wayback_dir=resolved_target,
            final_reference_path=resolved_final,
        )
        return {"cleaned": False, "reason": "FINAL_REFERENCE_MISSING", "bytes_freed": 0}

    if resolved_target == resolved_root or resolved_target.parent != resolved_root:
        _log_skipped(
            project_id=project_id,
            release_identifier=release_identifier,
            reason="UNSAFE_PATH",
            wayback_dir=resolved_target,
            final_reference_path=resolved_final,
        )
        return {"cleaned": False, "reason": "UNSAFE_PATH", "bytes_freed": 0}
    if not resolved_target.is_dir():
        _log_skipped(
            project_id=project_id,
            release_identifier=release_identifier,
            reason="PATH_NOT_FOUND",
            wayback_dir=resolved_target,
            final_reference_path=resolved_final,
        )
        return {"cleaned": False, "reason": "PATH_NOT_FOUND", "bytes_freed": 0}

    if _contains_active_reference(
        runtime_cache_root=runtime_cache_root.expanduser().resolve(),
        cache_key=resolved_target.name,
    ):
        _log_skipped(
            project_id=project_id,
            release_identifier=release_identifier,
            reason="ACTIVE_REFERENCE",
            wayback_dir=resolved_target,
            final_reference_path=resolved_final,
        )
        return {"cleaned": False, "reason": "ACTIVE_REFERENCE", "bytes_freed": 0}

    delete_targets = [resolved_target / name for name in COMPACTABLE_FILES if (resolved_target / name).is_file()]
    if not delete_targets:
        _log_skipped(
            project_id=project_id,
            release_identifier=release_identifier,
            reason="PATH_NOT_FOUND",
            wayback_dir=resolved_target,
            final_reference_path=resolved_final,
        )
        return {"cleaned": False, "reason": "PATH_NOT_FOUND", "bytes_freed": 0}

    bytes_freed = sum(_size_bytes(path) for path in delete_targets)
    for path in delete_targets:
        path.unlink()
    logger.info(
        "WAYBACK_MOSAIC_CACHE_CLEANED projectId=%s releaseIdentifier=%s waybackDir=%s finalReferencePath=%s bytesFreed=%s",
        project_id,
        release_identifier,
        resolved_target,
        resolved_final,
        bytes_freed,
    )
    return {"cleaned": True, "reason": None, "bytes_freed": bytes_freed}


def cleanup_finalized_temporal_project_wayback_mosaics(
    *,
    project: TemporalProject,
    settings: Settings,
) -> list[dict[str, Any]]:
    """Compact canonical-linked mosaics only after every project milestone completed."""

    if not settings.post_completion_request_cleanup_enabled:
        logger.info(
            "WAYBACK_MOSAIC_CACHE_CLEANUP_SKIPPED projectId=%s releaseIdentifier=%s reason=CONFIG_DISABLED",
            project.project_id,
            "*",
        )
        return []
    if not project.milestones or any(milestone.status != "complete" for milestone in project.milestones):
        return []

    project_dir = Path(project.project_dir) if project.project_dir else settings.temporal_projects_dir / project.project_id
    results: list[dict[str, Any]] = []
    seen_dirs: set[Path] = set()
    for milestone in project.milestones:
        reference = milestone.reference_imagery
        canonical_cog_path = Path(reference.canonical_cog_path) if reference and reference.canonical_cog_path else None
        if canonical_cog_path is None:
            continue
        canonical_metadata = read_reference_imagery_cache_metadata(canonical_cog_path.with_name("metadata.json"))
        source_dir = canonical_metadata.get("source_wayback_mosaic_dir") if canonical_metadata else None
        if not isinstance(source_dir, str) or not source_dir:
            _log_skipped(
                project_id=project.project_id,
                release_identifier=milestone.release_identifier,
                reason="PATH_NOT_FOUND",
                wayback_dir=settings.wayback_mosaic_cache_dir,
                final_reference_path=project_dir / "milestones" / milestone.release_identifier / "reference_imagery_cog.tif",
            )
            continue
        wayback_dir = Path(source_dir).expanduser().resolve()
        if wayback_dir in seen_dirs:
            continue
        seen_dirs.add(wayback_dir)
        final_reference_path = project_dir / "milestones" / milestone.release_identifier / "reference_imagery_cog.tif"
        try:
            results.append(
                cleanup_wayback_mosaic_cache_after_success(
                    project_id=project.project_id,
                    release_identifier=milestone.release_identifier,
                    wayback_mosaic_dir=wayback_dir,
                    final_reference_path=final_reference_path,
                    wayback_mosaics_root=settings.wayback_mosaic_cache_dir,
                    runtime_cache_root=settings.runtime_cache_dir,
                )
            )
        except Exception:
            logger.exception(
                "WAYBACK_MOSAIC_CACHE_CLEANUP_SKIPPED projectId=%s releaseIdentifier=%s reason=CLEANUP_ERROR waybackDir=%s finalReferencePath=%s",
                project.project_id,
                milestone.release_identifier,
                wayback_dir,
                final_reference_path,
            )
    return results
