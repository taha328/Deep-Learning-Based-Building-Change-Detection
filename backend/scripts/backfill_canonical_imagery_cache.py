#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

import rasterio
from rasterio.warp import transform_bounds

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import Settings  # noqa: E402
from src.domain.inference_reference_imagery import validate_canonical_cog_for_inference  # noqa: E402
from src.domain.reference_imagery_cache import (  # noqa: E402
    build_aoi_hash,
    build_reference_imagery_cache_key_payload,
    build_reference_imagery_cache_metadata,
    build_reference_imagery_key,
    read_reference_imagery_cache_metadata,
    reference_imagery_cache_cog_path,
    reference_imagery_cache_metadata_path,
    write_reference_imagery_cache_metadata,
)
from src.services.temporal_reference_imagery import REFERENCE_COG_FORMAT_VERSION, ensure_reference_imagery_cog  # noqa: E402

DEFAULT_MAX_ROWS = 200
CREATED_BY = "backfill_canonical_imagery_cache"


@dataclass(frozen=True)
class BackfillSource:
    source_id: str
    source_type: str
    source_raster_path: str
    source_valid_mask_path: str | None
    provider: str | None
    release_identifier: str | None
    release_num: int | None
    tile_matrix_set: str | None
    zoom: int | None
    tile_range: list[int] | None
    bounds_3857: list[float] | None
    aoi_hash: str | None
    source_project_id: str | None = None
    source_wayback_cache_key: str | None = None
    warnings: list[str] | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def runtime_cache_dir(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else (REPO_ROOT / "backend" / "runtime_cache").resolve()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def raster_metadata(path: Path) -> tuple[list[float] | None, int | None, list[str]]:
    warnings: list[str] = []
    try:
        with rasterio.open(path) as src:
            if src.crs is None:
                return None, None, ["missing_crs"]
            bounds_3857 = [float(src.bounds.left), float(src.bounds.bottom), float(src.bounds.right), float(src.bounds.top)]
            if str(src.crs).upper() != "EPSG:3857":
                try:
                    transformed = transform_bounds(src.crs, "EPSG:3857", *src.bounds, densify_pts=21)
                    bounds_3857 = [float(value) for value in transformed]
                    warnings.append("bounds_reprojected_to_epsg3857")
                except Exception as exc:  # noqa: BLE001
                    return None, None, [f"bounds_reprojection_failed:{type(exc).__name__}"]
            zoom = None
            if src.width > 0 and src.height > 0:
                warnings.append("tile_range_unknown_for_project_raster")
            return bounds_3857, zoom, warnings
    except Exception as exc:  # noqa: BLE001
        return None, None, [f"raster_open_failed:{type(exc).__name__}:{exc}"]


def normalize_tile_range(value: Any) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [int(item) for item in value]
    return None


def normalize_bounds(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [float(item) for item in value]
    return None


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def discover_wayback_sources(runtime: Path) -> list[BackfillSource]:
    sources: list[BackfillSource] = []
    root = runtime / "wayback_mosaics"
    if not root.is_dir():
        return sources
    for metadata_path in sorted(root.glob("*/metadata.json")):
        folder = metadata_path.parent
        metadata = read_json(metadata_path) or {}
        source_raster = folder / "mosaic.tif"
        valid_mask = folder / "valid_mask.tif"
        release_identifier = metadata.get("release_identifier")
        warnings: list[str] = []
        for field in ("release_identifier", "zoom", "tile_range", "bounds_3857"):
            if metadata.get(field) in (None, "", []):
                warnings.append(f"missing_{field}")
        sources.append(
            BackfillSource(
                source_id=str(folder),
                source_type="wayback_mosaic",
                source_raster_path=str(source_raster),
                source_valid_mask_path=str(valid_mask) if valid_mask.is_file() else None,
                provider="esri_wayback",
                release_identifier=release_identifier if isinstance(release_identifier, str) else None,
                release_num=int_or_none(metadata.get("release_num")),
                tile_matrix_set=metadata.get("tile_matrix_set") if isinstance(metadata.get("tile_matrix_set"), str) else None,
                zoom=int_or_none(metadata.get("zoom")),
                tile_range=normalize_tile_range(metadata.get("tile_range")),
                bounds_3857=normalize_bounds(metadata.get("bounds_3857")),
                aoi_hash=None,
                source_wayback_cache_key=folder.name,
                warnings=warnings,
            )
        )
    return sources


def discover_project_sources(runtime: Path, *, project_id: str | None) -> list[BackfillSource]:
    sources: list[BackfillSource] = []
    root = runtime / "temporal_projects"
    if not root.is_dir():
        return sources
    project_paths = [root / project_id / "project.json"] if project_id else sorted(root.glob("*/project.json"))
    for project_path in project_paths:
        payload = read_json(project_path)
        if not payload:
            continue
        current_project_id = payload.get("project_id") or project_path.parent.name
        if project_id and current_project_id != project_id and project_path.parent.name != project_id:
            continue
        aoi_hash = build_aoi_hash(payload.get("aoi_geojson") if isinstance(payload.get("aoi_geojson"), dict) else None)
        for milestone in payload.get("milestones", []):
            if not isinstance(milestone, dict):
                continue
            release_identifier = milestone.get("release_identifier")
            imagery = milestone.get("reference_imagery")
            if not isinstance(imagery, dict):
                continue
            cog_path = imagery.get("cog_path") or imagery.get("canonical_cog_path")
            if not isinstance(cog_path, str) or not cog_path:
                continue
            source_raster = Path(cog_path)
            bounds, _zoom, warnings = raster_metadata(source_raster)
            zoom = int_or_none(imagery.get("maxzoom"))
            sources.append(
                BackfillSource(
                    source_id=f"{current_project_id}:{release_identifier}",
                    source_type="temporal_project_cog",
                    source_raster_path=str(source_raster),
                    source_valid_mask_path=None,
                    provider="esri_wayback",
                    release_identifier=release_identifier if isinstance(release_identifier, str) else None,
                    release_num=int_or_none(imagery.get("release_num")),
                    tile_matrix_set=imagery.get("tile_matrix_set") if isinstance(imagery.get("tile_matrix_set"), str) else None,
                    zoom=zoom,
                    tile_range=normalize_tile_range(imagery.get("tile_range")),
                    bounds_3857=bounds,
                    aoi_hash=aoi_hash,
                    source_project_id=str(current_project_id),
                    warnings=warnings,
                )
            )
    return sources


def key_payload_for_source(source: BackfillSource) -> dict[str, object]:
    return build_reference_imagery_cache_key_payload(
        provider=source.provider or "esri_wayback",
        release_identifier=source.release_identifier or "",
        release_num=source.release_num,
        tile_matrix_set=source.tile_matrix_set,
        zoom=source.zoom,
        tile_range=source.tile_range,
        bounds_3857=source.bounds_3857,
        source_raster_path=None,
        valid_mask_path=None,
        aoi_hash=source.aoi_hash,
        reference_cog_format_version=REFERENCE_COG_FORMAT_VERSION,
    )


def source_missing_required_metadata(source: BackfillSource) -> list[str]:
    missing: list[str] = []
    for field in ("provider", "release_identifier", "zoom", "tile_range", "bounds_3857"):
        if getattr(source, field) in (None, "", []):
            missing.append(f"missing_{field}")
    return missing


def validation_payload(source: BackfillSource, reference_key: str, canonical_path: Path, metadata_path: Path, key_payload: dict[str, object], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    now = utc_now_iso()
    existing = read_reference_imagery_cache_metadata(metadata_path)
    metadata = build_reference_imagery_cache_metadata(
        reference_imagery_key=reference_key,
        key_payload=key_payload,
        canonical_cog_path=canonical_path,
        existing_metadata=existing,
    )
    metadata.update(
        {
            "source_type": source.source_type,
            "source_project_id": source.source_project_id,
            "source_release_identifier": source.release_identifier,
            "source_wayback_cache_key": source.source_wayback_cache_key,
            "source_wayback_mosaic_cache_key": source.source_wayback_cache_key,
            "source_raster_path": source.source_raster_path,
            "source_valid_mask_path": source.source_valid_mask_path,
            "canonical_valid_mask_path": str(canonical_path.with_name("valid_mask.tif")),
            "created_by": existing.get("created_by") if existing else CREATED_BY,
            "validated_at": now,
            "warnings": source.warnings or [],
        }
    )
    if extra:
        metadata.update(extra)
    return metadata


def validate_entry(source: BackfillSource, reference_key: str, canonical_path: Path, metadata_path: Path, key_payload: dict[str, object]) -> tuple[bool, dict[str, Any], str | None]:
    validation = validate_canonical_cog_for_inference(
        canonical_cog_path=canonical_path,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=key_payload,
        normalized_aoi=None,
    )
    return validation.valid, validation.diagnostics, validation.reason


def ensure_canonical_for_source(source: BackfillSource, runtime: Path, *, apply: bool) -> tuple[str, dict[str, Any] | None, str | None]:
    missing = source_missing_required_metadata(source)
    source_raster = Path(source.source_raster_path)
    if not source_raster.is_file():
        return "protected_missing_source", None, "source_raster_missing"
    if missing:
        return "protected_insufficient_metadata", None, ",".join(missing)

    key_payload = key_payload_for_source(source)
    reference_key = build_reference_imagery_key(key_payload)
    cache_dir = runtime / "imagery_cache"
    canonical_path = reference_imagery_cache_cog_path(cache_dir, reference_key)
    metadata_path = reference_imagery_cache_metadata_path(cache_dir, reference_key)

    if canonical_path.is_file() and metadata_path.is_file():
        valid, diagnostics, reason = validate_entry(source, reference_key, canonical_path, metadata_path, key_payload)
        if valid:
            if apply:
                metadata = validation_payload(source, reference_key, canonical_path, metadata_path, key_payload, {"validation": diagnostics})
                write_reference_imagery_cache_metadata(metadata_path, metadata)
            return "already_backfilled", {"reference_imagery_key": reference_key, "canonical_cog_path": str(canonical_path), "metadata_path": str(metadata_path), "validation": diagnostics}, None
        return "protected_invalid_source", {"reference_imagery_key": reference_key, "canonical_cog_path": str(canonical_path), "metadata_path": str(metadata_path), "validation": diagnostics}, reason

    expected_bytes = file_size(source_raster)
    candidate = {
        "reference_imagery_key": reference_key,
        "canonical_cog_path": str(canonical_path),
        "metadata_path": str(metadata_path),
        "expected_bytes_to_create": expected_bytes,
    }
    if not apply:
        return "backfill_candidate", candidate, None

    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ensure_reference_imagery_cog(
            source_raster,
            canonical_path,
            valid_mask_path=Path(source.source_valid_mask_path) if source.source_valid_mask_path else None,
            release_identifier=source.release_identifier,
        )
        metadata = validation_payload(source, reference_key, canonical_path, metadata_path, key_payload)
        write_reference_imagery_cache_metadata(metadata_path, metadata)
        valid, diagnostics, reason = validate_entry(source, reference_key, canonical_path, metadata_path, key_payload)
        metadata = validation_payload(source, reference_key, canonical_path, metadata_path, key_payload, {"validation": diagnostics})
        write_reference_imagery_cache_metadata(metadata_path, metadata)
        if not valid:
            return "protected_invalid_source", {**candidate, "validation": diagnostics}, reason
        return "created", {**candidate, "validation": diagnostics, "bytes_created": file_size(canonical_path)}, None
    except Exception as exc:  # noqa: BLE001
        return "error", candidate, f"{type(exc).__name__}:{exc}"


def inspect_sources(runtime: Path, *, project_id: str | None) -> list[BackfillSource]:
    return discover_project_sources(runtime, project_id=project_id) + discover_wayback_sources(runtime)


def source_report(source: BackfillSource, status: str, details: dict[str, Any] | None, reason: str | None) -> dict[str, Any]:
    return {
        **asdict(source),
        "status": status,
        "reason": reason,
        "details": details or {},
    }


def build_report(runtime: Path, *, apply: bool, yes: bool, project_id: str | None, max_rows: int) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if apply and not yes:
        errors.append({"error": "apply_requires_yes", "message": "--apply requires --yes; no files were written"})
        apply = False

    sources = inspect_sources(runtime, project_id=project_id)
    buckets: dict[str, list[dict[str, Any]]] = {
        "backfill_candidates": [],
        "already_backfilled": [],
        "canonical_cogs_created": [],
        "metadata_links_written": [],
        "protected_sources": [],
    }
    for source in sources:
        status, details, reason = ensure_canonical_for_source(source, runtime, apply=apply)
        row = source_report(source, status, details, reason)
        if status == "backfill_candidate":
            buckets["backfill_candidates"].append(row)
        elif status == "already_backfilled":
            buckets["already_backfilled"].append(row)
            if apply:
                buckets["metadata_links_written"].append(row)
        elif status == "created":
            buckets["canonical_cogs_created"].append(row)
            buckets["metadata_links_written"].append(row)
        elif status == "error":
            errors.append(row)
        else:
            buckets["protected_sources"].append(row)

    summary = {
        "sources_inspected_count": len(sources),
        "backfill_candidate_count": len(buckets["backfill_candidates"]),
        "already_backfilled_count": len(buckets["already_backfilled"]),
        "canonical_cogs_created_count": len(buckets["canonical_cogs_created"]),
        "metadata_links_written_count": len(buckets["metadata_links_written"]),
        "protected_source_count": len(buckets["protected_sources"]),
        "error_count": len(errors),
        "estimated_bytes_to_create": sum(int((row.get("details") or {}).get("expected_bytes_to_create") or 0) for row in buckets["backfill_candidates"]),
        "bytes_created": sum(int((row.get("details") or {}).get("bytes_created") or 0) for row in buckets["canonical_cogs_created"]),
    }
    next_steps = [
        "Run with --apply --yes only after reviewing dry-run candidates.",
        "Rerun compact_wayback_mosaics.py --dry-run after successful backfill.",
    ]
    return {
        "mode": "apply" if apply else "dry_run",
        "runtime_cache_dir": str(runtime),
        "summary": summary,
        "sources_inspected": [asdict(source) for source in sources[:max_rows]],
        "backfill_candidates": buckets["backfill_candidates"][:max_rows],
        "already_backfilled": buckets["already_backfilled"][:max_rows],
        "canonical_cogs_created": buckets["canonical_cogs_created"][:max_rows],
        "metadata_links_written": buckets["metadata_links_written"][:max_rows],
        "protected_sources": buckets["protected_sources"][:max_rows],
        "errors": errors[:max_rows],
        "next_steps": next_steps,
    }


def human_size(size_bytes: int | float) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], max_rows: int) -> str:
    if not rows:
        return "_None._"
    lines = ["| " + " | ".join(title for title, _key in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows[:max_rows]:
        values = []
        for _title, key in columns:
            value: Any = row
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if key.endswith("bytes_to_create") or key.endswith("bytes_created"):
                value = human_size(int(value or 0))
            values.append(str(value if value is not None else ""))
        lines.append("| " + " | ".join(" ".join(value.splitlines()) for value in values) + " |")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any], max_rows: int) -> str:
    summary = report["summary"]
    sections = [
        "# Canonical Imagery Cache Backfill Report — Phase 3.5",
        f"## Mode\n\n`{report['mode']}`",
        f"## Runtime Cache Location\n\n`{report['runtime_cache_dir']}`",
        "## Summary\n\n"
        f"- Sources inspected: {summary['sources_inspected_count']}\n"
        f"- Backfill candidates: {summary['backfill_candidate_count']}\n"
        f"- Already backfilled: {summary['already_backfilled_count']}\n"
        f"- Canonical COGs created: {summary['canonical_cogs_created_count']}\n"
        f"- Metadata links written: {summary['metadata_links_written_count']}\n"
        f"- Protected sources: {summary['protected_source_count']}\n"
        f"- Errors: {summary['error_count']}\n"
        f"- Estimated bytes to create: {human_size(summary['estimated_bytes_to_create'])}\n"
        f"- Bytes created: {human_size(summary['bytes_created'])}",
        "## Sources Inspected\n\n"
        + table(report["sources_inspected"], [("Source", "source_id"), ("Type", "source_type"), ("Release", "release_identifier")], max_rows),
        "## Backfill Candidates\n\n"
        + table(report["backfill_candidates"], [("Source", "source_id"), ("Release", "release_identifier"), ("Key", "details.reference_imagery_key"), ("Expected", "details.expected_bytes_to_create")], max_rows),
        "## Already Backfilled\n\n"
        + table(report["already_backfilled"], [("Source", "source_id"), ("Release", "release_identifier"), ("Key", "details.reference_imagery_key")], max_rows),
        "## Canonical COGs Created\n\n"
        + table(report["canonical_cogs_created"], [("Source", "source_id"), ("Release", "release_identifier"), ("Key", "details.reference_imagery_key"), ("Bytes", "details.bytes_created")], max_rows),
        "## Metadata Links Written\n\n"
        + table(report["metadata_links_written"], [("Source", "source_id"), ("Release", "release_identifier"), ("Key", "details.reference_imagery_key")], max_rows),
        "## Protected Sources\n\n"
        + table(report["protected_sources"], [("Source", "source_id"), ("Type", "source_type"), ("Release", "release_identifier"), ("Reason", "reason"), ("Status", "status")], max_rows),
        "## Errors\n\n" + table(report["errors"], [("Source", "source_id"), ("Reason", "reason"), ("Status", "status")], max_rows),
        "## Next Steps\n\n" + "\n".join(f"- {item}" for item in report["next_steps"]),
    ]
    return "\n\n".join(sections) + "\n"


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill canonical reference imagery COG cache from real runtime sources.")
    parser.add_argument("--runtime-cache-dir")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run mode. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Write canonical COGs and metadata. Requires --yes.")
    parser.add_argument("--yes", action="store_true", help="Confirm apply mode writes.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--project-id")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    runtime = runtime_cache_dir(args.runtime_cache_dir)
    Settings(runtime_cache_dir=runtime)
    report = build_report(runtime, apply=bool(args.apply), yes=bool(args.yes), project_id=args.project_id, max_rows=args.max_rows)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report, args.max_rows))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
