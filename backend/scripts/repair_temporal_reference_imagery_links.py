#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sys
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.domain.reference_imagery_cache import (  # noqa: E402
    append_reference_imagery_materialization,
    build_aoi_hash,
    materialize_reference_imagery_cog,
    read_reference_imagery_cache_metadata,
    write_reference_imagery_cache_metadata,
)
from src.services.temporal_projects import _reference_imagery_from_cog_path  # noqa: E402


REFERENCE_COG_FILENAME = "reference_imagery_cog.tif"
PROJECT_FILES = ("project.json", "project_manifest.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(temp_path, path)


def _release_identifier(milestone: dict[str, Any]) -> str | None:
    value = milestone.get("release_identifier") or milestone.get("releaseIdentifier") or milestone.get("identifier")
    return str(value) if value else None


def _project_id(payload: dict[str, Any], project_dir: Path) -> str:
    value = payload.get("project_id") or payload.get("id") or project_dir.name
    return str(value)


def _project_aoi_hash(payload: dict[str, Any]) -> str | None:
    aoi = payload.get("aoi_geojson") or payload.get("aoi") or payload.get("geometry")
    return build_aoi_hash(aoi if isinstance(aoi, dict) else None)


def _canonical_metadata_entries(runtime_cache_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for metadata_path in sorted((runtime_cache_dir / "imagery_cache").glob("*/metadata.json")):
        metadata = read_reference_imagery_cache_metadata(metadata_path)
        if metadata is None:
            continue
        key = str(metadata.get("reference_imagery_key") or metadata_path.parent.name)
        canonical_cog = Path(str(metadata.get("canonical_cog_path") or metadata_path.with_name(REFERENCE_COG_FILENAME)))
        if not canonical_cog.is_absolute():
            canonical_cog = metadata_path.parent / canonical_cog
        entries.append(
            {
                "reference_imagery_key": key,
                "release_identifier": metadata.get("release_identifier"),
                "aoi_hash": metadata.get("aoi_hash"),
                "canonical_cog_path": str(canonical_cog),
                "metadata_path": str(metadata_path),
                "metadata": metadata,
                "valid": canonical_cog.is_file() and canonical_cog.stat().st_size > 0,
            }
        )
    return entries


def _canonical_index(entries: list[dict[str, Any]]) -> dict[tuple[str | None, str | None], list[dict[str, Any]]]:
    index: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        index[(entry.get("aoi_hash"), entry.get("release_identifier"))].append(entry)
    return index


def _match_canonical_entry(
    *,
    milestone: dict[str, Any],
    project_id: str,
    release_identifier: str,
    aoi_hash: str | None,
    entries: list[dict[str, Any]],
    index: dict[tuple[str | None, str | None], list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    reference = milestone.get("reference_imagery")
    if isinstance(reference, dict):
        key = reference.get("reference_imagery_key")
        if key:
            keyed = [entry for entry in entries if entry.get("reference_imagery_key") == key]
            if len(keyed) == 1 and keyed[0].get("valid"):
                return keyed[0], None, keyed
            if len(keyed) > 1:
                return None, "protected_ambiguous_canonical_match", keyed
            return None, "protected_no_canonical_cog", keyed

    materialized = []
    for entry in entries:
        metadata = entry.get("metadata") or {}
        for item in metadata.get("materializations") or []:
            if not isinstance(item, dict):
                continue
            if item.get("project_id") == project_id and item.get("release_identifier") == release_identifier:
                materialized.append(entry)
                break
    materialized = [entry for entry in materialized if entry.get("valid")]
    if len(materialized) == 1:
        return materialized[0], None, materialized
    if len(materialized) > 1:
        return None, "protected_ambiguous_canonical_match", materialized

    candidates = [entry for entry in index.get((aoi_hash, release_identifier), []) if entry.get("valid")]
    if len(candidates) == 1:
        return candidates[0], None, candidates
    if len(candidates) > 1:
        return None, "protected_ambiguous_canonical_match", candidates
    return None, "protected_no_canonical_cog", candidates


def _reference_payload(
    *,
    project_id: str,
    release_identifier: str,
    canonical_entry: dict[str, Any],
    project_cog_path: Path,
    materialization_method: str,
    existing_reference: dict[str, Any] | None,
) -> dict[str, Any]:
    source_reference = None
    if existing_reference:
        from src.schemas import TemporalReferenceImagery

        try:
            source_reference = TemporalReferenceImagery.model_validate(existing_reference)
        except Exception:
            source_reference = None
    reference = _reference_imagery_from_cog_path(
        project_id=project_id,
        release_identifier=release_identifier,
        cog_path=project_cog_path,
        source_reference=source_reference,
        reference_imagery_key=str(canonical_entry["reference_imagery_key"]),
        canonical_cog_path=canonical_entry["canonical_cog_path"],
        materialization_method=materialization_method,
    )
    return reference.model_dump(mode="json")


def _has_usable_project_reference(milestone: dict[str, Any], project_cog_path: Path) -> bool:
    reference = milestone.get("reference_imagery")
    if not isinstance(reference, dict):
        return False
    cog_path = reference.get("cog_path")
    tilejson_url = reference.get("tilejson_url")
    tiles_url_template = reference.get("tiles_url_template")
    if not (tilejson_url and tiles_url_template):
        return False
    path = Path(str(cog_path)) if cog_path else project_cog_path
    return path.is_file()


def _update_project_files(
    *,
    project_dir: Path,
    release_identifier: str,
    reference_payload: dict[str, Any],
) -> list[str]:
    updated: list[str] = []
    for name in PROJECT_FILES:
        path = project_dir / name
        payload = _read_json(path)
        if payload is None:
            continue
        changed = False
        for milestone in payload.get("milestones") or []:
            if isinstance(milestone, dict) and _release_identifier(milestone) == release_identifier:
                milestone["reference_imagery"] = reference_payload
                changed = True
        if changed:
            _write_json_atomic(path, payload)
            updated.append(str(path))
    return updated


def build_report(
    *,
    runtime_cache_dir: Path,
    project_id: str | None = None,
    apply: bool = False,
    yes: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    runtime_cache_dir = runtime_cache_dir.expanduser().resolve()
    if apply and not yes:
        return {
            "mode": "apply-refused",
            "runtime_cache_dir": str(runtime_cache_dir),
            "summary": {"errors": 1},
            "projects_inspected": [],
            "milestones_inspected": [],
            "already_valid": [],
            "repair_candidates": [],
            "links_created": [],
            "metadata_updated": [],
            "protected_milestones": [],
            "errors": [{"reason": "apply_requires_yes", "message": "Apply mode requires --apply --yes."}],
            "next_steps": ["Re-run with --apply --yes only after reviewing dry-run output."],
        }

    entries = _canonical_metadata_entries(runtime_cache_dir)
    index = _canonical_index(entries)
    projects_root = runtime_cache_dir / "temporal_projects"
    project_dirs = [projects_root / project_id] if project_id else sorted(path.parent for path in projects_root.glob("*/project.json"))

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "runtime_cache_dir": str(runtime_cache_dir),
        "summary": {},
        "projects_inspected": [],
        "milestones_inspected": [],
        "already_valid": [],
        "repair_candidates": [],
        "links_created": [],
        "metadata_updated": [],
        "protected_milestones": [],
        "errors": [],
        "next_steps": [],
    }

    for project_dir in project_dirs:
        project_json = project_dir / "project.json"
        payload = _read_json(project_json)
        if payload is None:
            report["errors"].append({"project_dir": str(project_dir), "reason": "missing_or_invalid_project_json"})
            continue
        pid = _project_id(payload, project_dir)
        aoi_hash = _project_aoi_hash(payload)
        report["projects_inspected"].append({"project_id": pid, "path": str(project_dir), "aoi_hash": aoi_hash})
        for milestone in payload.get("milestones") or []:
            if not isinstance(milestone, dict):
                continue
            release = _release_identifier(milestone)
            inspected = {"project_id": pid, "release_identifier": release, "aoi_hash": aoi_hash}
            report["milestones_inspected"].append(inspected)
            if not release:
                report["protected_milestones"].append({**inspected, "reason": "protected_missing_release_identifier"})
                continue
            project_cog_path = project_dir / "milestones" / release / REFERENCE_COG_FILENAME
            if _has_usable_project_reference(milestone, project_cog_path):
                report["already_valid"].append({**inspected, "cog_path": str(project_cog_path)})
                continue
            entry, reject_reason, matches = _match_canonical_entry(
                milestone=milestone,
                project_id=pid,
                release_identifier=release,
                aoi_hash=aoi_hash,
                entries=entries,
                index=index,
            )
            if entry is None:
                report["protected_milestones"].append(
                    {
                        **inspected,
                        "reason": reject_reason or "protected_no_canonical_cog",
                        "candidate_count": len(matches),
                        "candidate_keys": [item.get("reference_imagery_key") for item in matches[:10]],
                    }
                )
                continue
            candidate = {
                **inspected,
                "reference_imagery_key": entry["reference_imagery_key"],
                "canonical_cog_path": entry["canonical_cog_path"],
                "project_cog_path": str(project_cog_path),
            }
            report["repair_candidates"].append(candidate)
            if not apply:
                continue
            try:
                materialized = materialize_reference_imagery_cog(
                    canonical_cog_path=Path(str(entry["canonical_cog_path"])),
                    project_cog_path=project_cog_path,
                )
                method = str(materialized.get("method") or "unknown")
                reference_payload = _reference_payload(
                    project_id=pid,
                    release_identifier=release,
                    canonical_entry=entry,
                    project_cog_path=project_cog_path,
                    materialization_method=method,
                    existing_reference=milestone.get("reference_imagery") if isinstance(milestone.get("reference_imagery"), dict) else None,
                )
                updated_files = _update_project_files(
                    project_dir=project_dir,
                    release_identifier=release,
                    reference_payload=reference_payload,
                )
                metadata = dict(entry["metadata"])
                append_reference_imagery_materialization(
                    metadata,
                    project_id=pid,
                    release_identifier=release,
                    project_cog_path=project_cog_path,
                    method=method,
                )
                write_reference_imagery_cache_metadata(Path(str(entry["metadata_path"])), metadata)
                report["links_created"].append({**candidate, "method": method})
                report["metadata_updated"].append(
                    {
                        **candidate,
                        "metadata_path": entry["metadata_path"],
                        "project_files": updated_files,
                    }
                )
            except Exception as exc:
                report["errors"].append({**candidate, "reason": exc.__class__.__name__, "message": str(exc)})

    for key in (
        "projects_inspected",
        "milestones_inspected",
        "already_valid",
        "repair_candidates",
        "links_created",
        "metadata_updated",
        "protected_milestones",
        "errors",
    ):
        report["summary"][key] = len(report[key])
        if max_rows is not None and max_rows >= 0:
            report[key] = report[key][:max_rows]
    if apply:
        report["next_steps"].append("Reload affected temporal projects and verify reference_imagery tilejson_url fields are present.")
    else:
        report["next_steps"].append("Review repair_candidates, then run again with --apply --yes to create compatibility links.")
    return report


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Temporal Reference Imagery Compatibility Repair",
        "",
        f"Mode: `{report['mode']}`",
        f"Runtime cache: `{report['runtime_cache_dir']}`",
        "",
        "## Summary",
    ]
    for key, value in report.get("summary", {}).items():
        lines.append(f"- {key}: {value}")
    for section in (
        "repair_candidates",
        "links_created",
        "metadata_updated",
        "already_valid",
        "protected_milestones",
        "errors",
        "next_steps",
    ):
        lines.extend(["", f"## {section}"])
        rows = report.get(section) or []
        if not rows:
            lines.append("- none")
            continue
        for row in rows:
            lines.append(f"- `{json.dumps(row, sort_keys=True, ensure_ascii=False)}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-cache-dir", type=Path, default=Path("runtime_cache"))
    parser.add_argument("--project-id")
    parser.add_argument("--dry-run", action="store_true", help="Default. Inspect and report without modifying files.")
    parser.add_argument("--apply", action="store_true", help="Create compatibility links and update metadata.")
    parser.add_argument("--yes", action="store_true", help="Required with --apply.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--max-rows", type=int, default=None)
    args = parser.parse_args(argv)

    report = build_report(
        runtime_cache_dir=args.runtime_cache_dir,
        project_id=args.project_id,
        apply=args.apply,
        yes=args.yes,
        max_rows=args.max_rows,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_markdown_report(report))
    return 1 if report["mode"] == "apply-refused" or report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
