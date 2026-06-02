#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
from typing import Any


DEFAULT_LARGE_JSON_THRESHOLD_MB = 50
DEFAULT_LARGE_FILE_THRESHOLD_MB = 100
DEFAULT_MAX_ROWS = 50

RUNTIME_SUBFOLDERS = (
    "requests",
    "temporal_projects",
    "wayback_mosaics",
    "reference_tiles",
    "temporal_vector_tiles",
    "qgis_artifacts",
    "tmp",
    "dev_client_logs",
    "db_payloads",
    "wayback_tiles",
    "wayback_tile_cache",
    "wayback_metadata_cache",
    "wayback_tile_preflight_cache",
    "wayback_releases",
    "mapbox_mosaics",
)

REQUIRED_MARKDOWN_SECTIONS = (
    "Summary",
    "Runtime Cache Location",
    "Largest Top-Level Directories",
    "Largest Files",
    "Temporal Project References",
    "Request Folder Analysis",
    "Wayback Mosaic Analysis",
    "Temporal Project Analysis",
    "Reference Imagery COG Analysis",
    "Derived Cache Analysis",
    "DB Payload Analysis",
    "Missing References",
    "Orphan Candidates",
    "Protected Artifacts",
    "Risk Notes",
    "Suggested Next Actions",
)


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_runtime_cache_dir(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()
    env_path = os.environ.get("APP_RUNTIME_CACHE_DIR")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (repo_root_from_script() / "backend" / "runtime_cache").resolve()


def size_path(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def count_files(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    count = 0
    for item in path.rglob("*"):
        if item.is_file():
            count += 1
    return count


def largest_files(path: Path, *, max_rows: int, min_size_bytes: int = 0) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
        except OSError:
            continue
        if size >= min_size_bytes:
            entries.append({"path": str(item), "size_bytes": size})
    entries.sort(key=lambda entry: int(entry["size_bytes"]), reverse=True)
    return entries[:max_rows]


def safe_json_load(path: Path, *, large_json_threshold_bytes: int) -> tuple[dict[str, Any] | None, str | None]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"stat_failed:{exc.__class__.__name__}"
    if size > large_json_threshold_bytes:
        return None, "large_json_skipped"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - inspection tool reports parse failures.
        return None, f"parse_failed:{exc.__class__.__name__}"
    if not isinstance(value, dict):
        return None, "json_not_object"
    return value, None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_release_identifier(milestone: dict[str, Any]) -> str | None:
    value = milestone.get("release_identifier") or milestone.get("releaseIdentifier")
    return str(value) if value else None


def normalize_project_id(project_dir: Path, payloads: list[dict[str, Any]]) -> str:
    for payload in payloads:
        value = payload.get("project_id") or payload.get("projectId") or payload.get("id")
        if value:
            return str(value)
    return project_dir.name


def resolve_reference_path(value: str, *, project_dir: Path, runtime_cache_dir: Path) -> Path | None:
    if not value or value.startswith(("http://", "https://", "/api/")):
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    candidates = [project_dir / path, runtime_cache_dir / path]
    return candidates[0] if candidates[0].exists() else candidates[-1]


def add_path_reference(
    references: dict[str, Any],
    value: Any,
    *,
    project_id: str,
    project_dir: Path,
    runtime_cache_dir: Path,
    source: str,
) -> None:
    if not isinstance(value, str) or not value:
        return
    path = resolve_reference_path(value, project_dir=project_dir, runtime_cache_dir=runtime_cache_dir)
    if path is None:
        references["urls"].append({"project_id": project_id, "source": source, "value": value})
        return
    entry = {"project_id": project_id, "source": source, "path": str(path), "exists": path.exists()}
    references["paths"].append(entry)
    if not path.exists():
        references["missing"].append(entry)
    else:
        references["protected"].append(entry)


def walk_project_payload_for_references(
    payload: dict[str, Any],
    *,
    project_id: str,
    project_dir: Path,
    runtime_cache_dir: Path,
    references: dict[str, Any],
) -> None:
    project_dir_value = payload.get("project_dir") or payload.get("projectDir")
    if isinstance(project_dir_value, str):
        add_path_reference(
            references,
            project_dir_value,
            project_id=project_id,
            project_dir=project_dir,
            runtime_cache_dir=runtime_cache_dir,
            source="project_dir",
        )
    for bundle_key in ("download_bundle_path", "downloadable_zip_path", "temporal_project_bundle_path"):
        add_path_reference(
            references,
            payload.get(bundle_key),
            project_id=project_id,
            project_dir=project_dir,
            runtime_cache_dir=runtime_cache_dir,
            source=bundle_key,
        )

    for milestone in as_list(payload.get("milestones")):
        if not isinstance(milestone, dict):
            continue
        release_identifier = normalize_release_identifier(milestone)
        pair_request_hash = milestone.get("pair_request_hash") or milestone.get("pairRequestHash")
        if isinstance(pair_request_hash, str) and pair_request_hash:
            references["request_hashes"].setdefault(pair_request_hash, []).append(
                {"project_id": project_id, "release_identifier": release_identifier}
            )
        imagery = milestone.get("reference_imagery") or milestone.get("referenceImagery")
        if isinstance(imagery, dict):
            for key in ("cog_path", "cogPath", "reference_imagery_cog"):
                add_path_reference(
                    references,
                    imagery.get(key),
                    project_id=project_id,
                    project_dir=project_dir,
                    runtime_cache_dir=runtime_cache_dir,
                    source=f"milestone:{release_identifier}:reference_imagery.{key}",
                )
            for key in ("cog_url", "cogUrl", "tilejson_url", "tilejsonUrl", "tiles_url_template", "tilesUrlTemplate"):
                add_path_reference(
                    references,
                    imagery.get(key),
                    project_id=project_id,
                    project_dir=project_dir,
                    runtime_cache_dir=runtime_cache_dir,
                    source=f"milestone:{release_identifier}:reference_imagery.{key}",
                )
        for artifact in as_list(milestone.get("artifacts")):
            if not isinstance(artifact, dict):
                continue
            artifact_key = artifact.get("key") or artifact.get("name") or "unknown"
            for key in (
                "path",
                "url",
                "artifact_url",
                "artifactUrl",
                "download_url",
                "downloadUrl",
                "geojson_url",
                "geojsonUrl",
                "gpkg_url",
                "gpkgUrl",
                "qgis_preferred_url",
                "qgisPreferredUrl",
            ):
                add_path_reference(
                    references,
                    artifact.get(key),
                    project_id=project_id,
                    project_dir=project_dir,
                    runtime_cache_dir=runtime_cache_dir,
                    source=f"milestone:{release_identifier}:artifact:{artifact_key}.{key}",
                )


def inspect_project_references(
    runtime_cache_dir: Path,
    *,
    large_json_threshold_bytes: int,
    max_rows: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    references: dict[str, Any] = {
        "request_hashes": {},
        "paths": [],
        "urls": [],
        "missing": [],
        "protected": [],
    }
    projects: list[dict[str, Any]] = []
    temporal_root = runtime_cache_dir / "temporal_projects"
    if not temporal_root.exists():
        return references, projects

    for project_dir in sorted(path for path in temporal_root.iterdir() if path.is_dir()):
        payloads: list[dict[str, Any]] = []
        skipped_json: list[dict[str, Any]] = []
        for name in ("project.json", "project_manifest.json", "project_summary.json"):
            path = project_dir / name
            if not path.is_file():
                continue
            payload, reason = safe_json_load(path, large_json_threshold_bytes=large_json_threshold_bytes)
            if payload is None:
                skipped_json.append({"path": str(path), "reason": reason, "size_bytes": size_path(path)})
            else:
                payloads.append(payload)
        ref_layers = project_dir / "reference_layers" / "reference_layers.json"
        if ref_layers.is_file():
            payload, reason = safe_json_load(ref_layers, large_json_threshold_bytes=large_json_threshold_bytes)
            if payload is None:
                skipped_json.append({"path": str(ref_layers), "reason": reason, "size_bytes": size_path(ref_layers)})
            else:
                payloads.append(payload)
        project_id = normalize_project_id(project_dir, payloads)
        for payload in payloads:
            walk_project_payload_for_references(
                payload,
                project_id=project_id,
                project_dir=project_dir,
                runtime_cache_dir=runtime_cache_dir,
                references=references,
            )
        cogs = largest_files(project_dir, max_rows=max_rows, min_size_bytes=0)
        cogs = [entry for entry in cogs if Path(str(entry["path"])).name == "reference_imagery_cog.tif"]
        geojsons = [entry for entry in largest_files(project_dir, max_rows=max_rows, min_size_bytes=0) if Path(str(entry["path"])).suffix == ".geojson"]
        project_json = project_dir / "project.json"
        project_manifest = project_dir / "project_manifest.json"
        project_summary = project_dir / "project_summary.json"
        milestone_dirs = sorted((project_dir / "milestones").iterdir()) if (project_dir / "milestones").exists() else []
        release_identifiers: list[str] = []
        for payload in payloads:
            for milestone in as_list(payload.get("milestones")):
                if isinstance(milestone, dict):
                    release = normalize_release_identifier(milestone)
                    if release and release not in release_identifiers:
                        release_identifiers.append(release)
        if not release_identifiers:
            release_identifiers = [path.name for path in milestone_dirs if path.is_dir()]
        large_metadata = [
            str(path)
            for path in (project_json, project_manifest, project_summary)
            if path.is_file() and size_path(path) > large_json_threshold_bytes
        ]
        projects.append(
            {
                "project_id": project_id,
                "path": str(project_dir),
                "folder_size_bytes": size_path(project_dir),
                "project_json_size_bytes": size_path(project_json),
                "project_manifest_size_bytes": size_path(project_manifest),
                "project_summary_size_bytes": size_path(project_summary),
                "milestone_count": len(release_identifiers),
                "release_identifiers": release_identifiers,
                "reference_imagery_cogs": cogs[:max_rows],
                "milestone_geojson_files": geojsons[:max_rows],
                "download_bundles": [entry for entry in largest_files(project_dir, max_rows=max_rows) if Path(str(entry["path"])).name.endswith(".zip")],
                "large_inline_json_risk": bool(large_metadata),
                "large_metadata_files": large_metadata,
                "skipped_json": skipped_json,
                "classification": "large_project_metadata_risk" if large_metadata else "durable_project_output",
                "reason": "temporal project folder contains user-visible project state and milestone artifacts",
            }
        )
    references["paths"] = references["paths"][: max_rows * 10]
    references["urls"] = references["urls"][: max_rows * 10]
    return references, projects


def inspect_requests(runtime_cache_dir: Path, references: dict[str, Any], *, max_rows: int) -> list[dict[str, Any]]:
    request_root = runtime_cache_dir / "requests"
    request_hashes = references.get("request_hashes", {})
    referenced_paths = [Path(item["path"]) for item in references.get("protected", []) if isinstance(item, dict) and item.get("path")]
    requests: list[dict[str, Any]] = []
    if not request_root.exists():
        return requests
    for request_dir in sorted(path for path in request_root.iterdir() if path.is_dir()):
        request_hash = request_dir.name
        files = largest_files(request_dir, max_rows=max_rows)
        has_path_reference = any(request_dir.resolve() == ref.resolve() or request_dir.resolve() in ref.resolve().parents for ref in referenced_paths)
        referenced_by_pair = request_hash in request_hashes
        protected = referenced_by_pair or has_path_reference
        requests.append(
            {
                "request_hash": request_hash,
                "path": str(request_dir),
                "folder_size_bytes": size_path(request_dir),
                "file_count": count_files(request_dir),
                "largest_files": files,
                "contains_run_response_json": (request_dir / "run_response.json").is_file(),
                "contains_manifest_json": (request_dir / "manifest.json").is_file(),
                "contains_timing_json": (request_dir / "timing.json").is_file(),
                "contains_export_bundle_zip": (request_dir / "export_bundle.zip").is_file(),
                "contains_prediction_change_probability_tif": (request_dir / "prediction_change_probability.tif").is_file(),
                "contains_prediction_change_mask_tif": (request_dir / "prediction_change_mask.tif").is_file(),
                "contains_building_change_polygons_geojson": (request_dir / "building_change_polygons.geojson").is_file(),
                "contains_building_change_polygons_geojsonl": (request_dir / "prediction_change_polygons.geojsonl").is_file()
                or (request_dir / "building_change_polygons.geojsonl").is_file(),
                "contains_tiled_inference_metadata_json": (request_dir / "tiled_inference_metadata.json").is_file(),
                "contains_tiles_directory": (request_dir / "tiles").is_dir(),
                "referenced_by_pair_request_hash": referenced_by_pair,
                "referenced_by_project_artifact_path": has_path_reference,
                "orphan_candidate": not protected,
                "classification": "protected_reference" if protected else "orphan_candidate",
                "reason": "referenced by temporal project metadata" if protected else "not referenced by inspected temporal project metadata",
            }
        )
    requests.sort(key=lambda item: int(item["folder_size_bytes"]), reverse=True)
    return requests


def inspect_wayback_mosaics(runtime_cache_dir: Path, *, large_json_threshold_bytes: int) -> list[dict[str, Any]]:
    root = runtime_cache_dir / "wayback_mosaics"
    mosaics: list[dict[str, Any]] = []
    if not root.exists():
        return mosaics
    for cache_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        metadata_path = cache_dir / "metadata.json"
        metadata, _reason = safe_json_load(metadata_path, large_json_threshold_bytes=large_json_threshold_bytes) if metadata_path.is_file() else (None, None)
        mosaic_tif = cache_dir / "mosaic.tif"
        mosaic_png = cache_dir / "mosaic.png"
        valid_mask = cache_dir / "valid_mask.tif"
        mosaics.append(
            {
                "cache_key": cache_dir.name,
                "path": str(cache_dir),
                "folder_size_bytes": size_path(cache_dir),
                "has_mosaic_tif": mosaic_tif.is_file(),
                "mosaic_tif_size_bytes": size_path(mosaic_tif),
                "has_mosaic_png": mosaic_png.is_file(),
                "mosaic_png_size_bytes": size_path(mosaic_png),
                "has_valid_mask_tif": valid_mask.is_file(),
                "valid_mask_tif_size_bytes": size_path(valid_mask),
                "has_metadata_json": metadata_path.is_file(),
                "metadata_release_identifier": (metadata or {}).get("release_identifier") or (metadata or {}).get("identifier"),
                "metadata_zoom": (metadata or {}).get("zoom"),
                "metadata_tile_range": (metadata or {}).get("tile_range"),
                "metadata_reusable": (metadata or {}).get("reusable"),
                "metadata_width": (metadata or {}).get("width"),
                "metadata_height": (metadata or {}).get("height"),
                "metadata_tile_count": (metadata or {}).get("tile_count"),
                "classification": "canonical_reusable_cache",
                "reason": "shared source/build imagery cache; Phase 0 does not mark wayback mosaics safe for deletion",
            }
        )
    mosaics.sort(key=lambda item: int(item["folder_size_bytes"]), reverse=True)
    return mosaics


def inspect_derived_cache(runtime_cache_dir: Path, *, max_rows: int) -> list[dict[str, Any]]:
    cache_specs = {
        "reference_tiles": ("derived_rebuildable_cache", "rendered from reference imagery COGs"),
        "temporal_vector_tiles": ("derived_rebuildable_cache", "rendered from temporal vector artifacts"),
        "qgis_artifacts": ("derived_rebuildable_cache", "download/cache copy of temporal vector artifacts for QGIS"),
        "tmp": ("temporary_workspace", "run scratch workspace"),
        "dev_client_logs": ("debug_only", "development client-log relay output"),
    }
    results: list[dict[str, Any]] = []
    for folder, (classification, reason) in cache_specs.items():
        path = runtime_cache_dir / folder
        results.append(
            {
                "name": folder,
                "path": str(path),
                "exists": path.exists(),
                "folder_size_bytes": size_path(path),
                "file_count": count_files(path),
                "largest_files": largest_files(path, max_rows=max_rows),
                "classification": classification,
                "reason": reason,
                "risk_level": "low" if classification == "derived_rebuildable_cache" else "medium",
            }
        )
    return results


def inspect_db_payloads(runtime_cache_dir: Path, *, max_rows: int) -> dict[str, Any]:
    root = runtime_cache_dir / "db_payloads"
    patterns = Counter()
    if root.exists():
        for file_path in root.rglob("*"):
            if file_path.is_file():
                try:
                    rel = file_path.relative_to(root)
                except ValueError:
                    continue
                parts = rel.parts[:2]
                patterns["/".join(parts) if parts else file_path.name] += 1
    return {
        "path": str(root),
        "exists": root.exists(),
        "folder_size_bytes": size_path(root),
        "file_count": count_files(root),
        "largest_files": largest_files(root, max_rows=max_rows),
        "path_patterns": [{"pattern": key, "count": count} for key, count in patterns.most_common(max_rows)],
        "classification": "unknown_risk",
        "reason": "database payload references require a separate persistence audit before cleanup",
    }


def top_level_directories(runtime_cache_dir: Path, *, max_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in RUNTIME_SUBFOLDERS:
        path = runtime_cache_dir / name
        rows.append(
            {
                "name": name,
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": size_path(path),
                "file_count": count_files(path),
            }
        )
    rows.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
    return rows[:max_rows]


def build_audit(
    runtime_cache_dir: Path,
    *,
    large_file_threshold_mb: int = DEFAULT_LARGE_FILE_THRESHOLD_MB,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict[str, Any]:
    large_json_threshold_bytes = DEFAULT_LARGE_JSON_THRESHOLD_MB * 1024 * 1024
    references, temporal_projects = inspect_project_references(
        runtime_cache_dir,
        large_json_threshold_bytes=large_json_threshold_bytes,
        max_rows=max_rows,
    )
    requests = inspect_requests(runtime_cache_dir, references, max_rows=max_rows)
    wayback_mosaics = inspect_wayback_mosaics(runtime_cache_dir, large_json_threshold_bytes=large_json_threshold_bytes)
    derived_caches = inspect_derived_cache(runtime_cache_dir, max_rows=max_rows)
    db_payloads = inspect_db_payloads(runtime_cache_dir, max_rows=max_rows)
    min_large_bytes = large_file_threshold_mb * 1024 * 1024
    largest = largest_files(runtime_cache_dir, max_rows=max_rows, min_size_bytes=min_large_bytes)
    all_files = largest_files(runtime_cache_dir, max_rows=10_000, min_size_bytes=0)
    reference_cogs = [entry for entry in all_files if Path(str(entry["path"])).name == "reference_imagery_cog.tif"][:max_rows]
    export_bundles = [entry for entry in all_files if Path(str(entry["path"])).name.endswith(".zip")][:max_rows]
    prediction_rasters = [
        entry
        for entry in all_files
        if Path(str(entry["path"])).name in {"prediction_change_probability.tif", "prediction_change_mask.tif", "change_probability.tif"}
    ][:max_rows]
    orphan_candidates = [
        {"type": "request_folder", "path": item["path"], "size_bytes": item["folder_size_bytes"], "reason": item["reason"]}
        for item in requests
        if item["orphan_candidate"]
    ]
    protected_artifacts = [
        {"type": "request_folder", "path": item["path"], "size_bytes": item["folder_size_bytes"], "reason": item["reason"]}
        for item in requests
        if not item["orphan_candidate"]
    ]
    protected_artifacts.extend(
        {"type": "referenced_path", "path": item["path"], "reason": item["source"], "project_id": item["project_id"]}
        for item in references.get("protected", [])
    )
    risk_notes = [
        "Do not delete wayback_mosaics blindly; they are classified as canonical_reusable_cache in Phase 0.",
        "Do not delete db_payloads without a separate DB reference audit.",
        "Large project JSON/manifest files indicate large_project_metadata_risk, not a cleanup permission.",
        "Request folders are protected when referenced by pair_request_hash or project artifact paths.",
    ]
    suggested_next_actions = [
        "rotate or truncate dev_client_logs after adding explicit log-retention behavior",
        "add canonical reference imagery cache in Phase 1",
        "add dry-run cleanup in Phase 2",
        "protect request folders referenced by temporal projects",
        "do not delete wayback_mosaics blindly",
        "inspect db_payload references before cleanup",
        "compact project JSON only after manifest-based frontend/QGIS compatibility is verified",
    ]
    return {
        "runtime_cache_dir": str(runtime_cache_dir),
        "summary": {
            "runtime_cache_exists": runtime_cache_dir.exists(),
            "runtime_cache_total_size_bytes": size_path(runtime_cache_dir),
            "file_count": count_files(runtime_cache_dir),
            "request_count": len(requests),
            "temporal_project_count": len(temporal_projects),
            "wayback_mosaic_count": len(wayback_mosaics),
            "missing_reference_count": len(references.get("missing", [])),
            "orphan_candidate_count": len(orphan_candidates),
            "protected_artifact_count": len(protected_artifacts),
        },
        "largest_directories": top_level_directories(runtime_cache_dir, max_rows=max_rows),
        "largest_files": largest,
        "temporal_project_references": references,
        "requests": requests[:max_rows],
        "wayback_mosaics": wayback_mosaics[:max_rows],
        "temporal_projects": temporal_projects[:max_rows],
        "reference_imagery_cogs": reference_cogs,
        "export_bundles": export_bundles,
        "prediction_rasters": prediction_rasters,
        "derived_caches": derived_caches,
        "db_payloads": db_payloads,
        "missing_references": references.get("missing", []),
        "orphan_candidates": orphan_candidates[:max_rows],
        "protected_artifacts": protected_artifacts[:max_rows],
        "risk_notes": risk_notes,
        "suggested_next_actions": suggested_next_actions,
    }


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
    header = "| " + " | ".join(title for title, _key in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]
    for row in rows[:max_rows]:
        values = []
        for _title, key in columns:
            value = row.get(key)
            if isinstance(value, bool):
                value = "yes" if value else "no"
            elif key.endswith("size_bytes") or key == "size_bytes" or key == "folder_size_bytes":
                value = human_size(int(value or 0))
            elif isinstance(value, list):
                value = ", ".join(str(item) for item in value[:5])
            values.append(str(value if value is not None else ""))
        lines.append("| " + " | ".join(" ".join(value.splitlines()) for value in values) + " |")
    return "\n".join(lines)


def render_markdown(audit: dict[str, Any], *, max_rows: int) -> str:
    summary = audit["summary"]
    sections: list[str] = ["# Runtime Storage Audit — building_change_app"]
    sections.append(
        "\n".join(
            [
                "## Summary",
                "",
                f"- Runtime cache total: {human_size(summary['runtime_cache_total_size_bytes'])}",
                f"- Files inspected: {summary['file_count']}",
                f"- Request folders: {summary['request_count']}",
                f"- Temporal projects: {summary['temporal_project_count']}",
                f"- Wayback mosaics: {summary['wayback_mosaic_count']}",
                f"- Missing references: {summary['missing_reference_count']}",
                f"- Orphan request candidates: {summary['orphan_candidate_count']}",
                f"- Protected artifacts/paths: {summary['protected_artifact_count']}",
            ]
        )
    )
    sections.append(f"## Runtime Cache Location\n\n`{audit['runtime_cache_dir']}`")
    sections.append(
        "## Largest Top-Level Directories\n\n"
        + md_table(audit["largest_directories"], [("Name", "name"), ("Exists", "exists"), ("Size", "size_bytes"), ("Files", "file_count")], max_rows=max_rows)
    )
    sections.append(
        "## Largest Files\n\n"
        + md_table(audit["largest_files"], [("Path", "path"), ("Size", "size_bytes")], max_rows=max_rows)
    )
    refs = audit["temporal_project_references"]
    sections.append(
        "\n".join(
            [
                "## Temporal Project References",
                "",
                f"- Referenced request hashes: {len(refs.get('request_hashes', {}))}",
                f"- Referenced file paths: {len(refs.get('paths', []))}",
                f"- Referenced URLs/API paths: {len(refs.get('urls', []))}",
            ]
        )
    )
    sections.append(
        "## Request Folder Analysis\n\n"
        + md_table(
            audit["requests"],
            [
                ("Request", "request_hash"),
                ("Size", "folder_size_bytes"),
                ("Files", "file_count"),
                ("Pair ref", "referenced_by_pair_request_hash"),
                ("Path ref", "referenced_by_project_artifact_path"),
                ("Orphan", "orphan_candidate"),
                ("Class", "classification"),
            ],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Wayback Mosaic Analysis\n\n"
        + md_table(
            audit["wayback_mosaics"],
            [
                ("Cache key", "cache_key"),
                ("Size", "folder_size_bytes"),
                ("mosaic.tif", "has_mosaic_tif"),
                ("TIF size", "mosaic_tif_size_bytes"),
                ("Release", "metadata_release_identifier"),
                ("Zoom", "metadata_zoom"),
                ("Class", "classification"),
            ],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Temporal Project Analysis\n\n"
        + md_table(
            audit["temporal_projects"],
            [
                ("Project", "project_id"),
                ("Size", "folder_size_bytes"),
                ("Milestones", "milestone_count"),
                ("project.json", "project_json_size_bytes"),
                ("manifest", "project_manifest_size_bytes"),
                ("Large JSON risk", "large_inline_json_risk"),
                ("Class", "classification"),
            ],
            max_rows=max_rows,
        )
    )
    sections.append(
        "## Reference Imagery COG Analysis\n\n"
        + md_table(audit["reference_imagery_cogs"], [("Path", "path"), ("Size", "size_bytes")], max_rows=max_rows)
    )
    sections.append(
        "## Derived Cache Analysis\n\n"
        + md_table(
            audit["derived_caches"],
            [("Name", "name"), ("Exists", "exists"), ("Size", "folder_size_bytes"), ("Files", "file_count"), ("Class", "classification"), ("Risk", "risk_level")],
            max_rows=max_rows,
        )
    )
    db_payloads = audit["db_payloads"]
    sections.append(
        "\n".join(
            [
                "## DB Payload Analysis",
                "",
                f"- Path: `{db_payloads['path']}`",
                f"- Exists: {db_payloads['exists']}",
                f"- Size: {human_size(db_payloads['folder_size_bytes'])}",
                f"- Files: {db_payloads['file_count']}",
                f"- Classification: `{db_payloads['classification']}`",
                f"- Reason: {db_payloads['reason']}",
            ]
        )
    )
    sections.append(
        "## Missing References\n\n"
        + md_table(audit["missing_references"], [("Project", "project_id"), ("Source", "source"), ("Path", "path")], max_rows=max_rows)
    )
    sections.append(
        "## Orphan Candidates\n\n"
        + md_table(audit["orphan_candidates"], [("Type", "type"), ("Path", "path"), ("Size", "size_bytes"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append(
        "## Protected Artifacts\n\n"
        + md_table(audit["protected_artifacts"], [("Type", "type"), ("Path", "path"), ("Size", "size_bytes"), ("Reason", "reason")], max_rows=max_rows)
    )
    sections.append("## Risk Notes\n\n" + "\n".join(f"- {item}" for item in audit["risk_notes"]))
    sections.append("## Suggested Next Actions\n\n" + "\n".join(f"- {item}" for item in audit["suggested_next_actions"]))
    return "\n\n".join(sections) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only runtime storage audit for building_change_app.")
    parser.add_argument("--runtime-cache-dir", help="Runtime cache directory to inspect.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--large-file-threshold-mb", type=int, default=DEFAULT_LARGE_FILE_THRESHOLD_MB)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime_cache_dir = resolve_runtime_cache_dir(args.runtime_cache_dir)
    if not runtime_cache_dir.exists() or not runtime_cache_dir.is_dir():
        message = f"Runtime cache directory not found: {runtime_cache_dir}"
        if args.json:
            print(json.dumps({"error": message, "runtime_cache_dir": str(runtime_cache_dir)}, indent=2))
        else:
            print(f"# Runtime Storage Audit — building_change_app\n\n{message}", file=sys.stderr)
        return 2
    audit = build_audit(
        runtime_cache_dir,
        large_file_threshold_mb=args.large_file_threshold_mb,
        max_rows=max(args.max_rows, 1),
    )
    if args.json:
        print(json.dumps(audit, indent=2, sort_keys=True))
    else:
        print(render_markdown(audit, max_rows=max(args.max_rows, 1)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
