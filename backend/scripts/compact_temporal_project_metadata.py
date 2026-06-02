#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


DEFAULT_MAX_INLINE_GEOJSON_BYTES = 1_000_000
DEFAULT_MAX_ROWS = 50
MAX_METADATA_PARSE_BYTES = 1024 * 1024 * 1024
VECTOR_TILE_METADATA_THRESHOLD_BYTES = 10_000_000
QGIS_GPKG_CONVERSION_VERSION = "gpkg1"
QGIS_EMPTY_GEOJSON_THRESHOLD_BYTES = 256
TEMPORAL_VECTOR_TILE_SOURCE_LAYER = "results"

REQUIRED_MARKDOWN_SECTIONS = (
    "Mode",
    "Runtime Cache Location",
    "Summary",
    "Projects Scanned",
    "Metadata Files",
    "Artifacts Externalized",
    "Artifacts Reused",
    "References Preserved",
    "Backups",
    "Actions Taken",
    "Errors",
    "Next Steps",
)

TEMPORAL_LAYER_ARTIFACTS: dict[str, tuple[str, str, str, str]] = {
    "automated_additions": (
        "automated_additions_geojson",
        "automated_additions.geojson",
        "Automated additions footprint",
        "application/geo+json",
    ),
    "automated_candidate_footprint": (
        "automated_candidate_footprint_geojson",
        "automated_candidate_footprint.geojson",
        "Automated cumulative candidate footprint",
        "application/geo+json",
    ),
    "automated_building_blocks": (
        "automated_building_blocks_geojson",
        "automated_building_blocks.geojson",
        "Automated building-level blocks",
        "application/geo+json",
    ),
    "manual_override": ("manual_override_geojson", "manual_override.geojson", "Manual milestone override", "application/geo+json"),
    "additions": ("additions_geojson", "additions.geojson", "Effective additions since previous milestone", "application/geo+json"),
    "effective_building_blocks": (
        "effective_building_blocks_geojson",
        "effective_building_blocks.geojson",
        "Grouped blocks built from effective additions",
        "application/geo+json",
    ),
    "effective_footprint": (
        "effective_footprint_geojson",
        "effective_footprint.geojson",
        "Effective footprint at this milestone",
        "application/geo+json",
    ),
    "building_change_buffer_10m": (
        "buffer_layers_geojson.10m",
        "building_change_buffer_10m.geojson",
        "Building-change buffer 10 m",
        "application/geo+json",
    ),
    "building_change_buffer_15m": (
        "buffer_layers_geojson.15m",
        "building_change_buffer_15m.geojson",
        "Building-change buffer 15 m",
        "application/geo+json",
    ),
    "building_change_buffer_20m": (
        "buffer_layers_geojson.20m",
        "building_change_buffer_20m.geojson",
        "Building-change buffer 20 m",
        "application/geo+json",
    ),
    "cumulative_union": ("cumulative_union_geojson", "cumulative_union.geojson", "Cumulative union up to this milestone", "application/geo+json"),
    "cumulative_convex_hull": (
        "cumulative_convex_hull_geojson",
        "cumulative_convex_hull.geojson",
        "Convex hull of cumulative union up to this milestone",
        "application/geo+json",
    ),
    "cumulative_growth_blocks": (
        "cumulative_growth_blocks_geojson",
        "cumulative_growth_blocks.geojson",
        "Grouped blocks built from cumulative union",
        "application/geo+json",
    ),
    "cumulative_growth_envelope": (
        "cumulative_growth_envelope_geojson",
        "cumulative_growth_envelope.geojson",
        "Smoothed cumulative growth envelope",
        "application/geo+json",
    ),
    "change_polygons": ("change_polygons_geojson", "change_polygons.geojson", "Pairwise change polygons", "application/geo+json"),
    "building_blocks": ("building_blocks_geojson", "building_blocks.geojson", "Pairwise building blocks", "application/geo+json"),
}

PRESERVED_REFERENCE_KEYS = (
    "pair_request_hash",
    "reference_imagery",
    "cog_path",
    "canonical_cog_path",
    "reference_imagery_key",
    "download_bundle_path",
    "warnings",
    "metrics",
    "status",
    "release_identifier",
    "release_date",
)


@dataclass(frozen=True)
class MetadataFileResult:
    path: str
    before_bytes: int
    after_bytes: int
    changed: bool
    parse_error: str | None = None


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_runtime_cache_dir(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()
    env_path = os.environ.get("APP_RUNTIME_CACHE_DIR")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (repo_root_from_script() / "backend" / "runtime_cache").resolve()


def utc_now_label() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def human_size(size_bytes: int | float) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def stable_json_bytes(payload: Any, *, indent: int | None = None) -> bytes:
    if indent is None:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=indent)
    return (text + "\n").encode("utf-8")


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            tmp_path.unlink()


def read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"stat_failed:{exc.__class__.__name__}"
    if size > MAX_METADATA_PARSE_BYTES:
        return None, "metadata_too_large_to_parse_safely"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reports per-file parse failures and keeps scanning.
        return None, f"parse_failed:{exc.__class__.__name__}"
    if not isinstance(value, dict):
        return None, "json_not_object"
    return value, None


def normalize_project_id(project_dir: Path, payload: dict[str, Any] | None = None) -> str:
    if payload:
        value = payload.get("project_id") or payload.get("projectId") or payload.get("id")
        if isinstance(value, str) and value:
            return value
    return project_dir.name


def normalize_release_identifier(milestone: dict[str, Any]) -> str | None:
    value = milestone.get("release_identifier") or milestone.get("releaseIdentifier")
    return str(value) if value else None


def artifact_path_for_milestone(project_dir: Path, release_identifier: str, filename: str) -> Path:
    return project_dir / "milestones" / release_identifier / filename


def feature_collection_size(payload: dict[str, Any]) -> int:
    return len(stable_json_bytes(payload))


def get_payload(milestone: dict[str, Any], field_path: str) -> dict[str, Any] | None:
    if field_path.startswith("buffer_layers_geojson."):
        key = field_path.split(".", 1)[1]
        buffers = milestone.get("buffer_layers_geojson")
        if isinstance(buffers, dict):
            payload = buffers.get(key) or buffers.get(key.rstrip("m")) or buffers.get(key.replace("m", " m"))
            return payload if isinstance(payload, dict) else None
        return None
    payload = milestone.get(field_path)
    return payload if isinstance(payload, dict) else None


def clear_payload(milestone: dict[str, Any], field_path: str) -> None:
    if field_path.startswith("buffer_layers_geojson."):
        key = field_path.split(".", 1)[1]
        buffers = milestone.get("buffer_layers_geojson")
        if isinstance(buffers, dict):
            for candidate in (key, key.rstrip("m"), key.replace("m", " m")):
                buffers.pop(candidate, None)
            milestone["buffer_layers_geojson"] = buffers
        return
    if field_path in milestone:
        milestone[field_path] = None


def iter_coords(value: Any):
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            yield float(value[0]), float(value[1])
            return
        for child in value:
            yield from iter_coords(child)


def geojson_metadata(payload: dict[str, Any]) -> tuple[int | None, list[float] | None]:
    features = payload.get("features")
    if not isinstance(features, list):
        return None, None
    bounds: list[float] | None = None
    for feature in features:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        for x, y in iter_coords(coords):
            if bounds is None:
                bounds = [x, y, x, y]
            else:
                bounds = [min(bounds[0], x), min(bounds[1], y), max(bounds[2], x), max(bounds[3], y)]
    return len(features), bounds


def geojson_file_metadata(path: Path) -> tuple[int | None, list[float] | None]:
    try:
        if path.stat().st_size > VECTOR_TILE_METADATA_THRESHOLD_BYTES:
            return None, None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    return geojson_metadata(payload) if isinstance(payload, dict) else (None, None)


def is_empty_qgis_geojson_artifact(path: Path, feature_count: int | None, size_bytes: int | None) -> bool:
    if feature_count == 0:
        return True
    if feature_count is None and size_bytes is not None and size_bytes <= QGIS_EMPTY_GEOJSON_THRESHOLD_BYTES:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        features = payload.get("features") if isinstance(payload, dict) else None
        return isinstance(features, list) and len(features) == 0
    return False


def should_advertise_vector_tiles(feature_count: int | None, size_bytes: int | None) -> bool:
    return (feature_count or 0) >= 20_000 or (size_bytes or 0) >= VECTOR_TILE_METADATA_THRESHOLD_BYTES


def build_artifact_entry(
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    path: Path,
    description: str,
    media_type: str,
) -> dict[str, Any]:
    size_bytes = path.stat().st_size if path.is_file() else None
    feature_count, bbox = geojson_file_metadata(path) if media_type == "application/geo+json" else (None, None)
    artifact_url = f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}"
    geojson_url = f"{artifact_url}.geojson" if media_type == "application/geo+json" else None
    empty_qgis_artifact = (
        media_type == "application/geo+json"
        and path.is_file()
        and is_empty_qgis_geojson_artifact(path, feature_count, size_bytes)
    )
    gpkg_url = f"{artifact_url}.gpkg" if media_type == "application/geo+json" and not empty_qgis_artifact else None
    source_mtime_ns = path.stat().st_mtime_ns if path.is_file() else None
    tilejson_url = None
    tiles_url_template = None
    vector_source_layer = None
    if media_type == "application/geo+json" and should_advertise_vector_tiles(feature_count, size_bytes):
        tilejson_url = f"{artifact_url}/tilejson.json"
        tiles_url_template = f"{artifact_url}/tiles/{{z}}/{{x}}/{{y}}.mvt"
        vector_source_layer = TEMPORAL_VECTOR_TILE_SOURCE_LAYER
    return {
        "name": f"{release_identifier}_{artifact_key}",
        "path": str(path),
        "media_type": media_type,
        "description": description,
        "key": artifact_key,
        "feature_count": feature_count,
        "size_bytes": size_bytes,
        "source_mtime_ns": source_mtime_ns,
        "qgis_cache_key": f"{source_mtime_ns}-{size_bytes}-{QGIS_GPKG_CONVERSION_VERSION}" if gpkg_url and source_mtime_ns is not None and size_bytes is not None else None,
        "bbox": bbox,
        "sha256": sha256_file(path),
        "artifact_url": artifact_url,
        "geojson_url": geojson_url,
        "download_url": geojson_url or artifact_url,
        "gpkg_url": gpkg_url,
        "qgis_preferred_url": None if empty_qgis_artifact else (gpkg_url or geojson_url or artifact_url),
        "qgis_preferred_format": "gpkg" if gpkg_url else None,
        "qgis_compatible": media_type == "application/geo+json" and not empty_qgis_artifact,
        "tilejson_url": tilejson_url,
        "tiles_url_template": tiles_url_template,
        "vector_source_layer": vector_source_layer,
    }


def snapshot_preserved_values(payload: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "download_bundle_path": payload.get("download_bundle_path"),
        "warnings": payload.get("warnings"),
    }
    milestones = []
    for milestone in payload.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        milestones.append(
            {
                "release_identifier": milestone.get("release_identifier"),
                "release_date": milestone.get("release_date"),
                "status": milestone.get("status"),
                "pair_request_hash": milestone.get("pair_request_hash"),
                "reference_imagery": copy.deepcopy(milestone.get("reference_imagery")),
                "warnings": copy.deepcopy(milestone.get("warnings")),
                "metrics": copy.deepcopy(milestone.get("metrics")),
            }
        )
    values["milestones"] = milestones
    return values


def externalize_payload(
    *,
    project_id: str,
    project_dir: Path,
    payload: dict[str, Any],
    max_inline_geojson_bytes: int,
    apply: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    compacted = copy.deepcopy(payload)
    stats: dict[str, Any] = {
        "artifacts_externalized": [],
        "artifacts_reused": [],
        "artifacts_preserved_inline": [],
        "artifact_write_actions": [],
        "bytes_externalized": 0,
        "references_preserved": [],
        "errors": [],
    }
    before_references = snapshot_preserved_values(compacted)
    milestones = compacted.get("milestones")
    if not isinstance(milestones, list):
        return compacted, stats

    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        release_identifier = normalize_release_identifier(milestone)
        if not release_identifier:
            continue
        artifacts = milestone.get("artifacts")
        if not isinstance(artifacts, list):
            artifacts = []
        artifacts_by_key = {
            artifact.get("key"): artifact
            for artifact in artifacts
            if isinstance(artifact, dict) and artifact.get("key")
        }
        for artifact_key, (field_path, filename, description, media_type) in TEMPORAL_LAYER_ARTIFACTS.items():
            inline_payload = get_payload(milestone, field_path)
            artifact_path = artifact_path_for_milestone(project_dir, release_identifier, filename)
            should_clear = False
            if isinstance(inline_payload, dict) and inline_payload.get("type") == "FeatureCollection":
                size_bytes = feature_collection_size(inline_payload)
                if size_bytes <= max_inline_geojson_bytes:
                    stats["artifacts_preserved_inline"].append(
                        {
                            "project_id": project_id,
                            "release_identifier": release_identifier,
                            "artifact_key": artifact_key,
                            "inline_size_bytes": size_bytes,
                            "reason": "below_inline_threshold",
                        }
                    )
                    continue
                should_clear = True
                stats["bytes_externalized"] += size_bytes
                if artifact_path.is_file():
                    stats["artifacts_reused"].append(
                        {
                            "project_id": project_id,
                            "release_identifier": release_identifier,
                            "artifact_key": artifact_key,
                            "path": str(artifact_path),
                            "reason": "existing_external_artifact",
                        }
                    )
                else:
                    stats["artifacts_externalized"].append(
                        {
                            "project_id": project_id,
                            "release_identifier": release_identifier,
                            "artifact_key": artifact_key,
                            "path": str(artifact_path),
                            "inline_size_bytes": size_bytes,
                            "reason": "inline_feature_collection_above_threshold",
                        }
                    )
                    stats["artifact_write_actions"].append(
                        {
                            "action": "write_geojson",
                            "path": str(artifact_path),
                            "size_bytes": size_bytes,
                            "applied": apply,
                        }
                    )
                    if apply:
                        atomic_write_bytes(artifact_path, stable_json_bytes(inline_payload, indent=2))
                if artifact_path.is_file() or apply:
                    target_path = artifact_path
                    if target_path.is_file():
                        artifacts_by_key[artifact_key] = build_artifact_entry(
                            project_id=project_id,
                            release_identifier=release_identifier,
                            artifact_key=artifact_key,
                            path=target_path,
                            description=description,
                            media_type=media_type,
                        )
                if apply and artifact_path.is_file():
                    artifacts_by_key[artifact_key] = build_artifact_entry(
                        project_id=project_id,
                        release_identifier=release_identifier,
                        artifact_key=artifact_key,
                        path=artifact_path,
                        description=description,
                        media_type=media_type,
                    )
                elif not apply:
                    estimated_path = artifact_path
                    artifacts_by_key[artifact_key] = {
                        **artifacts_by_key.get(artifact_key, {}),
                        "name": f"{release_identifier}_{artifact_key}",
                        "path": str(estimated_path),
                        "media_type": media_type,
                        "description": description,
                        "key": artifact_key,
                        "artifact_url": f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}",
                        "geojson_url": f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}.geojson",
                        "download_url": f"/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}.geojson",
                    }
            if should_clear:
                clear_payload(milestone, field_path)
        milestone["artifacts"] = list(artifacts_by_key.values())
    after_references = snapshot_preserved_values(compacted)
    for key in PRESERVED_REFERENCE_KEYS:
        if key in {"cog_path", "canonical_cog_path", "reference_imagery_key", "metrics", "status", "release_identifier", "release_date", "pair_request_hash"}:
            continue
        if before_references.get(key) == after_references.get(key):
            stats["references_preserved"].append(key)
    if before_references.get("milestones") == after_references.get("milestones"):
        stats["references_preserved"].extend(
            [
                "milestone_pair_request_hash",
                "milestone_reference_imagery",
                "milestone_status",
                "milestone_warnings",
                "milestone_metrics",
                "release_metadata",
            ]
        )
    else:
        stats["errors"].append({"error": "preserved_reference_changed", "details": "milestone reference/status/metric fields changed"})
    return compacted, stats


def metadata_files_for_project(project_dir: Path) -> list[Path]:
    candidates = [project_dir / "project.json", project_dir / "project_manifest.json", project_dir / "project_summary.json"]
    return [path for path in candidates if path.is_file()]


def backup_file(path: Path, *, suffix: str) -> Path:
    backup_path = path.with_name(f"{path.name}.bak-{suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def compact_project(
    project_dir: Path,
    *,
    max_inline_geojson_bytes: int,
    apply: bool,
    backup: bool,
) -> dict[str, Any]:
    primary_path = project_dir / "project.json"
    primary_payload, primary_error = read_json_file(primary_path) if primary_path.is_file() else (None, "missing_project_json")
    project_id = normalize_project_id(project_dir, primary_payload)
    result: dict[str, Any] = {
        "project_id": project_id,
        "project_dir": str(project_dir),
        "metadata_files": [],
        "artifacts_externalized": [],
        "artifacts_reused": [],
        "artifacts_preserved_inline": [],
        "references_preserved": [],
        "backups": [],
        "actions_taken": [],
        "errors": [],
    }
    if primary_error is not None or primary_payload is None:
        result["errors"].append({"path": str(primary_path), "error": primary_error})
        return result

    compacted, stats = externalize_payload(
        project_id=project_id,
        project_dir=project_dir,
        payload=primary_payload,
        max_inline_geojson_bytes=max_inline_geojson_bytes,
        apply=apply,
    )
    for key in (
        "artifacts_externalized",
        "artifacts_reused",
        "artifacts_preserved_inline",
        "references_preserved",
        "artifact_write_actions",
        "errors",
    ):
        if key == "artifact_write_actions":
            result["actions_taken"].extend(stats[key])
        elif key == "errors":
            result["errors"].extend(stats[key])
        else:
            result[key].extend(stats[key])

    suffix = f"metadata_compaction_{utc_now_label()}"
    for path in metadata_files_for_project(project_dir):
        before_bytes = path.stat().st_size
        if path.name in {"project.json", "project_manifest.json"}:
            next_payload = copy.deepcopy(compacted)
        else:
            payload, error = read_json_file(path)
            if error is not None or payload is None:
                result["metadata_files"].append(
                    asdict(MetadataFileResult(str(path), before_bytes, before_bytes, False, error))
                )
                result["errors"].append({"path": str(path), "error": error})
                continue
            next_payload, summary_stats = externalize_payload(
                project_id=project_id,
                project_dir=project_dir,
                payload=payload,
                max_inline_geojson_bytes=max_inline_geojson_bytes,
                apply=apply,
            )
            result["artifacts_externalized"].extend(summary_stats["artifacts_externalized"])
            result["artifacts_reused"].extend(summary_stats["artifacts_reused"])
            result["artifacts_preserved_inline"].extend(summary_stats["artifacts_preserved_inline"])
            result["references_preserved"].extend(summary_stats["references_preserved"])
            result["errors"].extend(summary_stats["errors"])
            if not summary_stats["artifacts_externalized"] and not summary_stats["artifacts_reused"]:
                result["metadata_files"].append(
                    asdict(MetadataFileResult(str(path), before_bytes, before_bytes, False, None))
                )
                continue
        next_bytes = stable_json_bytes(next_payload, indent=2)
        changed = len(next_bytes) != before_bytes
        if not changed:
            changed = next_bytes != path.read_bytes()
        if changed and apply:
            if backup:
                backup_path = backup_file(path, suffix=suffix)
                result["backups"].append({"path": str(backup_path), "source": str(path)})
            atomic_write_bytes(path, next_bytes)
            result["actions_taken"].append({"action": "write_metadata", "path": str(path), "size_bytes": len(next_bytes), "applied": True})
        elif changed:
            result["actions_taken"].append({"action": "write_metadata", "path": str(path), "size_bytes": len(next_bytes), "applied": False})
        result["metadata_files"].append(
            asdict(MetadataFileResult(str(path), before_bytes, len(next_bytes), changed, None))
        )
    result["references_preserved"] = sorted(set(result["references_preserved"]))
    return result


def discover_projects(runtime_cache_dir: Path, *, project_id: str | None) -> list[Path]:
    temporal_root = runtime_cache_dir / "temporal_projects"
    if project_id:
        return [temporal_root / project_id] if (temporal_root / project_id).is_dir() else []
    if not temporal_root.is_dir():
        return []
    return sorted(path for path in temporal_root.iterdir() if path.is_dir() and (path / "project.json").is_file())


def build_report(
    runtime_cache_dir: Path,
    *,
    project_id: str | None,
    apply: bool,
    yes: bool,
    backup: bool,
    max_inline_geojson_bytes: int,
    max_rows: int,
) -> dict[str, Any]:
    mode = "apply" if apply else "dry_run"
    errors: list[dict[str, Any]] = []
    if apply and not yes:
        errors.append({"error": "apply_requires_yes", "message": "--apply requires --yes; no files were changed"})
        apply_effective = False
    else:
        apply_effective = apply

    projects = []
    for project_dir in discover_projects(runtime_cache_dir, project_id=project_id):
        projects.append(
            compact_project(
                project_dir,
                max_inline_geojson_bytes=max_inline_geojson_bytes,
                apply=apply_effective,
                backup=backup,
            )
        )
    summary = {
        "runtime_cache_exists": runtime_cache_dir.exists(),
        "project_count": len(projects),
        "metadata_file_count": sum(len(project["metadata_files"]) for project in projects),
        "changed_metadata_file_count": sum(1 for project in projects for item in project["metadata_files"] if item["changed"]),
        "artifacts_externalized_count": sum(len(project["artifacts_externalized"]) for project in projects),
        "artifacts_reused_count": sum(len(project["artifacts_reused"]) for project in projects),
        "artifacts_preserved_inline_count": sum(len(project["artifacts_preserved_inline"]) for project in projects),
        "actions_taken_count": sum(len(project["actions_taken"]) for project in projects),
        "backup_count": sum(len(project["backups"]) for project in projects),
        "max_inline_geojson_bytes": max_inline_geojson_bytes,
    }
    report = {
        "mode": mode,
        "runtime_cache_dir": str(runtime_cache_dir),
        "apply_effective": apply_effective,
        "project_filter": project_id,
        "summary": summary,
        "projects": projects[:max_rows],
        "metadata_files": [item for project in projects for item in project["metadata_files"]][:max_rows],
        "artifacts_externalized": [item for project in projects for item in project["artifacts_externalized"]][:max_rows],
        "artifacts_reused": [item for project in projects for item in project["artifacts_reused"]][:max_rows],
        "artifacts_preserved_inline": [item for project in projects for item in project["artifacts_preserved_inline"]][:max_rows],
        "references_preserved": sorted({item for project in projects for item in project["references_preserved"]}),
        "backups": [item for project in projects for item in project["backups"]][:max_rows],
        "actions_taken": [item for project in projects for item in project["actions_taken"]][:max_rows],
        "errors": (errors + [item for project in projects for item in project["errors"]])[:max_rows],
        "next_steps": [
            "Review dry-run metadata and artifact actions before applying.",
            "Use --apply --yes --backup for writes after dry-run review.",
            "Run Phase 0/2 storage inspection again after compaction.",
        ],
    }
    return report


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
            elif key.endswith("bytes") or key in {"before_bytes", "after_bytes", "size_bytes"}:
                value = human_size(int(value or 0))
            values.append(str(value if value is not None else ""))
        lines.append("| " + " | ".join(" ".join(value.splitlines()) for value in values) + " |")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any], *, max_rows: int) -> str:
    summary = report["summary"]
    lines = [
        f"## Mode\n{report['mode']} (apply_effective={report['apply_effective']})",
        f"## Runtime Cache Location\n`{report['runtime_cache_dir']}`",
        "## Summary\n"
        + "\n".join(
            f"- {key}: {value}" for key, value in summary.items()
        ),
        "## Projects Scanned\n"
        + md_table(
            report["projects"],
            [("Project", "project_id"), ("Path", "project_dir")],
            max_rows=max_rows,
        ),
        "## Metadata Files\n"
        + md_table(
            report["metadata_files"],
            [("Path", "path"), ("Before", "before_bytes"), ("After", "after_bytes"), ("Changed", "changed"), ("Error", "parse_error")],
            max_rows=max_rows,
        ),
        "## Artifacts Externalized\n"
        + md_table(
            report["artifacts_externalized"],
            [("Project", "project_id"), ("Release", "release_identifier"), ("Key", "artifact_key"), ("Path", "path"), ("Reason", "reason")],
            max_rows=max_rows,
        ),
        "## Artifacts Reused\n"
        + md_table(
            report["artifacts_reused"],
            [("Project", "project_id"), ("Release", "release_identifier"), ("Key", "artifact_key"), ("Path", "path"), ("Reason", "reason")],
            max_rows=max_rows,
        ),
        "## References Preserved\n" + (", ".join(report["references_preserved"]) if report["references_preserved"] else "_None reported._"),
        "## Backups\n"
        + md_table(report["backups"], [("Path", "path"), ("Source", "source")], max_rows=max_rows),
        "## Actions Taken\n"
        + md_table(
            report["actions_taken"],
            [("Action", "action"), ("Path", "path"), ("Size", "size_bytes"), ("Applied", "applied")],
            max_rows=max_rows,
        ),
        "## Errors\n" + (json.dumps(report["errors"], indent=2) if report["errors"] else "_None._"),
        "## Next Steps\n" + "\n".join(f"- {item}" for item in report["next_steps"]),
    ]
    return "\n\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact temporal project metadata by externalizing large inline GeoJSON payloads.")
    parser.add_argument("--runtime-cache-dir", default=None)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run; this is also the default.")
    parser.add_argument("--apply", action="store_true", help="Write compacted metadata and artifact files.")
    parser.add_argument("--yes", action="store_true", help="Required with --apply.")
    parser.add_argument("--backup", action="store_true", help="Create .bak files before metadata writes.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--max-inline-geojson-bytes", type=int, default=DEFAULT_MAX_INLINE_GEOJSON_BYTES)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    runtime_cache_dir = resolve_runtime_cache_dir(args.runtime_cache_dir)
    report = build_report(
        runtime_cache_dir,
        project_id=args.project_id,
        apply=bool(args.apply),
        yes=bool(args.yes),
        backup=bool(args.backup),
        max_inline_geojson_bytes=args.max_inline_geojson_bytes,
        max_rows=args.max_rows,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report, max_rows=args.max_rows))
    if args.apply and not args.yes:
        return 2
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
