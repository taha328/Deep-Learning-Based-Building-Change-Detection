#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.domain.inference_reference_imagery import validate_canonical_cog_for_inference  # noqa: E402
from src.domain.reference_imagery_cache import read_reference_imagery_cache_metadata  # noqa: E402

DEFAULT_OLDER_THAN_HOURS = 72
DEFAULT_MAX_ROWS = 200
LARGE_JSON_THRESHOLD_BYTES = 50 * 1024 * 1024
UNKNOWN_RISK_SCAN_CHUNK_BYTES = 1024 * 1024
COMPACTABLE_FILES = ("mosaic.tif", "mosaic.png")
RETAINED_FILES = ("metadata.json", "valid_mask.tif")
FORBIDDEN_DIRS = {
    "imagery_cache",
    "temporal_projects",
    "requests",
    "db_payloads",
    "wayback_tile_cache",
    "wayback_tiles",
    "mapbox_mosaics",
    "wayback_metadata_cache",
    "wayback_tile_preflight_cache",
    "wayback_releases",
}


@dataclass(frozen=True)
class MosaicEntry:
    cache_key: str
    mosaic_dir: str
    size_bytes: int
    age_hours: float
    protected: bool
    protection_reasons: list[str]
    release_identifier: str | None = None
    release_num: int | None = None
    tile_matrix_set: str | None = None
    zoom: int | None = None
    tile_range: list[int] | None = None
    bounds_3857: list[float] | None = None
    mosaic_tif_path: str | None = None
    mosaic_png_path: str | None = None
    valid_mask_path: str | None = None
    metadata_path: str | None = None
    canonical_reference_imagery_key: str | None = None
    canonical_cog_path: str | None = None
    canonical_metadata_path: str | None = None
    canonical_validation_status: str | None = None
    source_references: list[dict[str, str]] | None = None
    missing_metadata_fields: list[str] | None = None
    delete_targets: list[str] | None = None

    @property
    def path(self) -> str:
        return self.mosaic_dir

    @property
    def reasons(self) -> list[str]:
        return self.protection_reasons


def runtime_cache_dir(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else (REPO_ROOT / "backend" / "runtime_cache").resolve()


def size_path(path: Path) -> int:
    if not path.exists():
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


def age_hours(path: Path, now: datetime) -> float:
    return max((now - datetime.fromtimestamp(latest_mtime(path), UTC)).total_seconds() / 3600.0, 0.0)


def read_json_if_small(path: Path) -> tuple[Any | None, str | None]:
    try:
        if path.stat().st_size > LARGE_JSON_THRESHOLD_BYTES:
            return None, "unknown_metadata"
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # noqa: BLE001
        return None, f"unknown_metadata:{type(exc).__name__}"


def iter_json_strings(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_json_strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from iter_json_strings(child)
        return
    if isinstance(value, str):
        yield value


def wayback_cache_keys_from_value(value: Any) -> set[str]:
    keys: set[str] = set()
    marker = "wayback_mosaics/"
    for text in iter_json_strings(value):
        normalized = text.replace("\\", "/")
        start = 0
        while True:
            index = normalized.find(marker, start)
            if index < 0:
                break
            tail = normalized[index + len(marker) :]
            parts = tail.split("/", 2)
            if len(parts) >= 2 and parts[1] in COMPACTABLE_FILES and parts[0]:
                keys.add(parts[0])
            start = index + len(marker)
    return keys


def is_backup_metadata_path(path: Path) -> bool:
    normalized_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    if any(part in {"backups", "backup", ".backup", "metadata_backups"} for part in normalized_parts):
        return True
    backup_markers = (".bak-", ".backup-", ".bak.", ".backup.", "~")
    return any(marker in name for marker in backup_markers) or name.endswith((".bak", ".backup", ".orig"))


def reference_kind_for_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "unknown"
    first = relative.parts[0] if relative.parts else ""
    if first == "requests":
        return "request"
    if first == "temporal_projects":
        return "project"
    return "unknown"


def reference_reason_for_path(path: Path, root: Path) -> str:
    kind = reference_kind_for_path(path, root)
    if kind == "request":
        return "referenced_by_request_metadata"
    if kind == "project":
        return "referenced_by_project_metadata"
    return "referenced_by_reference_imagery_image_path"


def collect_mosaic_references(root: Path) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    references: dict[str, list[dict[str, str]]] = {}
    unknown: list[dict[str, Any]] = []
    for folder in (root / "requests", root / "temporal_projects"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.json"):
            if is_backup_metadata_path(path):
                continue
            payload, error = read_json_if_small(path)
            if error is not None:
                unknown.append(
                    {
                        "path": str(path),
                        "size_bytes": size_path(path),
                        "reason": "unknown_large_metadata" if error == "unknown_metadata" else error,
                        "detail": "large_or_unparseable_request_or_project_metadata",
                    }
                )
                continue
            for cache_key in wayback_cache_keys_from_value(payload):
                references.setdefault(cache_key, []).append(
                    {
                        "path": str(path),
                        "kind": reference_kind_for_path(path, root),
                        "reason": reference_reason_for_path(path, root),
                    }
                )
    return references, unknown


def _relative_path_tokens(path: Path, runtime: Path) -> list[str]:
    tokens = [str(path), path.as_posix()]
    try:
        relative = path.relative_to(runtime)
        tokens.append(relative.as_posix())
    except ValueError:
        pass
    return sorted(set(tokens), key=len, reverse=True)


def build_unknown_scan_needles(runtime: Path, mosaic_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    needles: dict[str, dict[str, Any]] = {}
    for folder in mosaic_dirs:
        cache_key = folder.name
        values = {
            cache_key,
            f"wayback_mosaics/{cache_key}",
            f"source_wayback_cache_key\":\"{cache_key}",
            f"source_wayback_mosaic_cache_key\":\"{cache_key}",
            f"source_wayback_cache_key': '{cache_key}",
            f"source_wayback_mosaic_cache_key': '{cache_key}",
        }
        for file_name in COMPACTABLE_FILES:
            candidate = folder / file_name
            values.update(_relative_path_tokens(candidate, runtime))
        for value in values:
            if value:
                needles[value] = {"cache_key": cache_key, "value": value}
    return needles


def scan_unknown_risk_file(path: Path, needles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    size = size_path(path)
    result: dict[str, Any] = {
        "path": str(path),
        "size_bytes": size,
        "classification": "no_wayback_reference_found",
        "matched_cache_keys": [],
        "matched_paths": [],
        "blocked_candidate_cache_keys": [],
        "reason": "large_json_scanned_without_wayback_reference",
    }
    if not path.is_file():
        result.update({"classification": "scan_failed", "reason": "unknown_risk_file_missing"})
        return result
    try:
        max_needle_len = max((len(value) for value in needles), default=0)
        overlap = max(max_needle_len - 1, 0)
        carry = ""
        matched_values: set[str] = set()
        matched_cache_keys: set[str] = set()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(UNKNOWN_RISK_SCAN_CHUNK_BYTES)
                if not chunk:
                    break
                text = carry + chunk.decode("utf-8", errors="ignore").replace("\\", "/")
                for needle, metadata in needles.items():
                    if needle in text:
                        matched_values.add(str(metadata["value"]))
                        matched_cache_keys.add(str(metadata["cache_key"]))
                carry = text[-overlap:] if overlap else ""
        if matched_cache_keys:
            result.update(
                {
                    "classification": "references_other_mosaic",
                    "matched_cache_keys": sorted(matched_cache_keys),
                    "matched_paths": sorted(matched_values),
                    "reason": "unknown_risk_file_references_wayback_mosaic",
                }
            )
        return result
    except UnicodeError:
        result.update({"classification": "unsupported_binary", "reason": "unknown_risk_binary_scan_failed"})
        return result
    except Exception as exc:  # noqa: BLE001
        result.update({"classification": "scan_failed", "reason": f"unknown_risk_scan_failed:{type(exc).__name__}:{exc}"})
        return result


def scan_unknown_risk_items(
    unknown_items: list[dict[str, Any]],
    *,
    runtime: Path,
    mosaic_dirs: list[Path],
    candidate_cache_keys: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    needles = build_unknown_scan_needles(runtime, mosaic_dirs)
    scanned: list[dict[str, Any]] = []
    candidate_blockers: list[dict[str, Any]] = []
    blocker_reasons_by_key: dict[str, list[str]] = {}
    for item in unknown_items:
        scan = scan_unknown_risk_file(Path(str(item.get("path"))), needles)
        scan.update({key: value for key, value in item.items() if key not in scan})
        matched = set(scan.get("matched_cache_keys") or [])
        candidate_matches = sorted(matched & candidate_cache_keys)
        if candidate_matches:
            scan["classification"] = "references_candidate_mosaic"
            scan["blocked_candidate_cache_keys"] = candidate_matches
            scan["reason"] = "unknown_risk_file_references_candidate_mosaic"
            for cache_key in candidate_matches:
                blockers = [
                    {
                        "kind": "unknown_risk_reference",
                        "path": scan["path"],
                        "matched_value": matched_value,
                    }
                    for matched_value in scan.get("matched_paths", [])
                    if cache_key in str(matched_value)
                ]
                if not blockers:
                    blockers = [{"kind": "unknown_risk_reference", "path": scan["path"], "matched_value": cache_key}]
                candidate_blockers.append({"cache_key": cache_key, "blockers": blockers})
                blocker_reasons_by_key.setdefault(cache_key, []).append("referenced_by_unknown_risk_metadata")
        elif scan["classification"] in {"scan_failed", "unsupported_binary", "too_large_unscanned"}:
            scan["blocked_candidate_cache_keys"] = sorted(candidate_cache_keys)
            for cache_key in candidate_cache_keys:
                candidate_blockers.append(
                    {
                        "cache_key": cache_key,
                        "blockers": [{"kind": "unknown_risk_scan_failure", "path": scan["path"], "matched_value": scan["reason"]}],
                    }
                )
                blocker_reasons_by_key.setdefault(cache_key, []).append("unknown_risk_scan_failed")
        else:
            scan["blocked_candidate_cache_keys"] = []
        scanned.append(scan)
    return scanned, candidate_blockers, blocker_reasons_by_key


def unknown_risk_summary(scanned: list[dict[str, Any]]) -> dict[str, int]:
    keys = [
        "references_candidate_mosaic",
        "references_other_mosaic",
        "no_wayback_reference_found",
        "scan_failed",
        "too_large_unscanned",
        "unsupported_binary",
    ]
    summary = {"total": len(scanned), **{key: 0 for key in keys}}
    for item in scanned:
        classification = str(item.get("classification") or "scan_failed")
        summary[classification] = summary.get(classification, 0) + 1
    return summary


def canonical_metadata_by_wayback_cache(runtime: Path) -> dict[str, list[dict[str, Any]]]:
    results: dict[str, list[dict[str, Any]]] = {}
    cache_root = runtime / "imagery_cache"
    if not cache_root.is_dir():
        return results
    for metadata_path in cache_root.glob("*/metadata.json"):
        metadata = read_reference_imagery_cache_metadata(metadata_path)
        if not metadata:
            continue
        cache_key = metadata.get("source_wayback_mosaic_cache_key") or metadata.get("source_wayback_cache_key")
        if isinstance(cache_key, str) and cache_key:
            results.setdefault(cache_key, []).append({"metadata_path": metadata_path, "metadata": metadata})
    return results


def normalize_tile_range(value: Any) -> list[int] | None:
    if isinstance(value, dict):
        candidates = (("x_min", "x_max", "y_min", "y_max"), ("min_x", "max_x", "min_y", "max_y"))
        for keys in candidates:
            if all(isinstance(value.get(key), (int, float)) for key in keys):
                return [int(value[key]) for key in keys]
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [int(item) for item in value]
    return None


def normalize_bounds(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        candidates = (("left", "bottom", "right", "top"), ("minx", "miny", "maxx", "maxy"), ("west", "south", "east", "north"))
        lowered = {str(key).lower(): val for key, val in value.items()}
        for keys in candidates:
            if all(isinstance(lowered.get(key), (int, float)) for key in keys):
                return [float(lowered[key]) for key in keys]
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


def mosaic_metadata(folder: Path) -> tuple[dict[str, Any], list[str], str | None]:
    metadata_path = folder / "metadata.json"
    if not metadata_path.is_file():
        return {}, ["missing_wayback_metadata"], "missing_wayback_metadata"
    payload, error = read_json_if_small(metadata_path)
    if error is not None or not isinstance(payload, dict):
        return {}, ["missing_wayback_metadata"], error or "missing_wayback_metadata"
    missing: list[str] = []
    if not isinstance(payload.get("release_identifier"), str) or not payload.get("release_identifier"):
        missing.append("missing_release_identifier")
    if int_or_none(payload.get("zoom")) is None:
        missing.append("missing_zoom")
    if normalize_tile_range(payload.get("tile_range") or payload.get("tileRange")) is None:
        missing.append("missing_tile_range")
    if normalize_bounds(payload.get("bounds_3857") or payload.get("bounds3857") or payload.get("bbox_3857") or payload.get("bounds")) is None:
        missing.append("missing_bounds_3857")
    return payload, missing, None


def validate_linked_canonical(metadata_path: Path, metadata: dict[str, Any]) -> tuple[bool, str | None, str | None, str | None, str | None]:
    reference_key = metadata.get("reference_imagery_key")
    canonical_path = metadata.get("canonical_cog_path")
    if not isinstance(reference_key, str) or not isinstance(canonical_path, str):
        return False, "missing_canonical_cog", None, reference_key if isinstance(reference_key, str) else None, "missing_canonical_cog"
    if not metadata_path.is_file():
        return False, "missing_canonical_metadata", canonical_path, reference_key, "missing_canonical_metadata"
    if not Path(canonical_path).with_name("valid_mask.tif").is_file():
        return False, "missing_valid_mask", canonical_path, reference_key, "missing_valid_mask"
    required_payload = {
        key: metadata.get(key)
        for key in (
            "provider",
            "release_identifier",
            "release_num",
            "tile_matrix_set",
            "zoom",
            "tile_range",
            "bounds_3857",
            "aoi_hash",
            "reference_cog_format_version",
        )
    }
    validation = validate_canonical_cog_for_inference(
        canonical_cog_path=Path(canonical_path),
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=required_payload,
        normalized_aoi=None,
    )
    if not validation.valid:
        return False, "canonical_cog_validation_failed", canonical_path, reference_key, validation.reason or "canonical_cog_validation_failed"
    if validation.valid_mask_path is None or not validation.valid_mask_path.is_file():
        return False, "missing_valid_mask", canonical_path, reference_key, "missing_valid_mask"
    return True, None, canonical_path, reference_key, "valid"


def reason_counts(entries: list[MosaicEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        for reason in entry.protection_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def entry_dict(entry: MosaicEntry) -> dict[str, Any]:
    payload = asdict(entry)
    payload["path"] = entry.mosaic_dir
    payload["reasons"] = list(entry.protection_reasons)
    return payload


def classify_mosaics(runtime: Path, *, older_than_hours: int) -> tuple[list[MosaicEntry], list[MosaicEntry], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    now = datetime.now(UTC)
    wayback_root = runtime / "wayback_mosaics"
    linked = canonical_metadata_by_wayback_cache(runtime)
    references, unknown = collect_mosaic_references(runtime)
    if not wayback_root.is_dir():
        return [], [], unknown, [], unknown_risk_summary(unknown)
    mosaic_dirs = sorted(path for path in wayback_root.iterdir() if path.is_dir())
    records: list[dict[str, Any]] = []
    for folder in mosaic_dirs:
        cache_key = folder.name
        reasons: list[str] = []
        delete_targets = [str(folder / name) for name in COMPACTABLE_FILES if (folder / name).is_file()]
        folder_age = age_hours(folder, now)
        metadata, missing_metadata_fields, metadata_error = mosaic_metadata(folder)
        if folder_age < older_than_hours:
            reasons.append("ttl_not_met")
        if metadata_error:
            reasons.append("missing_wayback_metadata")
        reasons.extend(missing_metadata_fields)
        if not (folder / "valid_mask.tif").is_file():
            reasons.append("missing_valid_mask")
        sources = references.get(cache_key, [])
        for source in sources:
            reasons.append(source["reason"])
        linked_entries = linked.get(cache_key, [])
        canonical_cog_path: str | None = None
        canonical_metadata_path: str | None = None
        canonical_reference_imagery_key: str | None = None
        canonical_validation_status: str | None = None
        if not linked_entries:
            reasons.append("missing_canonical_cog")
        elif len(linked_entries) > 1:
            reasons.append("ambiguous_metadata_link")
            canonical_validation_status = "ambiguous_metadata_link"
            canonical_metadata_path = str(linked_entries[0]["metadata_path"])
        else:
            any_valid = False
            last_reason = "canonical_cog_validation_failed"
            for item in linked_entries:
                valid, reason, candidate_cog_path, reference_key, validation_status = validate_linked_canonical(item["metadata_path"], item["metadata"])
                canonical_cog_path = candidate_cog_path
                canonical_metadata_path = str(item["metadata_path"])
                canonical_reference_imagery_key = reference_key
                canonical_validation_status = validation_status
                if valid:
                    any_valid = True
                    break
                last_reason = reason or last_reason
            if not any_valid:
                reasons.append(last_reason)
        if not delete_targets:
            reasons.append("no_compactable_files")

        records.append(
            {
                "cache_key": cache_key,
                "folder": folder,
                "size_bytes": sum(size_path(Path(target)) for target in delete_targets),
                "age_hours": round(folder_age, 2),
                "reasons": reasons,
                "metadata": metadata,
                "mosaic_tif_path": str(folder / "mosaic.tif") if (folder / "mosaic.tif").is_file() else None,
                "mosaic_png_path": str(folder / "mosaic.png") if (folder / "mosaic.png").is_file() else None,
                "valid_mask_path": str(folder / "valid_mask.tif") if (folder / "valid_mask.tif").is_file() else None,
                "metadata_path": str(folder / "metadata.json") if (folder / "metadata.json").is_file() else None,
                "canonical_reference_imagery_key": canonical_reference_imagery_key,
                "canonical_cog_path": canonical_cog_path,
                "canonical_metadata_path": canonical_metadata_path,
                "canonical_validation_status": canonical_validation_status,
                "sources": sources,
                "missing_metadata_fields": missing_metadata_fields,
                "delete_targets": delete_targets,
            }
        )
    candidate_cache_keys = {str(record["cache_key"]) for record in records if not record["reasons"]}
    scanned_unknown, candidate_blockers, unknown_reasons_by_key = scan_unknown_risk_items(
        unknown,
        runtime=runtime,
        mosaic_dirs=mosaic_dirs,
        candidate_cache_keys=candidate_cache_keys,
    )
    protected: list[MosaicEntry] = []
    candidates: list[MosaicEntry] = []
    for record in records:
        reasons = [*record["reasons"], *unknown_reasons_by_key.get(str(record["cache_key"]), [])]
        metadata = record["metadata"]
        folder = record["folder"]
        entry = MosaicEntry(
            cache_key=record["cache_key"],
            mosaic_dir=str(folder),
            size_bytes=record["size_bytes"],
            age_hours=record["age_hours"],
            protected=bool(reasons),
            protection_reasons=sorted(set(reasons)),
            release_identifier=metadata.get("release_identifier") if isinstance(metadata.get("release_identifier"), str) else None,
            release_num=int_or_none(metadata.get("release_num")),
            tile_matrix_set=metadata.get("tile_matrix_set") if isinstance(metadata.get("tile_matrix_set"), str) else None,
            zoom=int_or_none(metadata.get("zoom")),
            tile_range=normalize_tile_range(metadata.get("tile_range") or metadata.get("tileRange")),
            bounds_3857=normalize_bounds(metadata.get("bounds_3857") or metadata.get("bounds3857") or metadata.get("bbox_3857") or metadata.get("bounds")),
            mosaic_tif_path=record["mosaic_tif_path"],
            mosaic_png_path=record["mosaic_png_path"],
            valid_mask_path=record["valid_mask_path"],
            metadata_path=record["metadata_path"],
            canonical_reference_imagery_key=record["canonical_reference_imagery_key"],
            canonical_cog_path=record["canonical_cog_path"],
            canonical_metadata_path=record["canonical_metadata_path"],
            canonical_validation_status=record["canonical_validation_status"],
            source_references=record["sources"],
            missing_metadata_fields=record["missing_metadata_fields"],
            delete_targets=record["delete_targets"],
        )
        if entry.protection_reasons:
            protected.append(entry)
        else:
            candidates.append(entry)
    return protected, candidates, scanned_unknown, candidate_blockers, unknown_risk_summary(scanned_unknown)


def verify_delete_target(path: Path, runtime: Path) -> str | None:
    resolved = path.resolve()
    if resolved.name not in COMPACTABLE_FILES:
        return "unsupported_delete_target"
    if resolved.parent.parent != (runtime / "wayback_mosaics").resolve():
        return "target_outside_wayback_mosaic_entry"
    if any(part in resolved.parts for part in FORBIDDEN_DIRS if part != "wayback_mosaics"):
        return "target_inside_forbidden_area"
    return None


def apply_candidates(candidates: list[MosaicEntry], runtime: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        for target in candidate.delete_targets or []:
            path = Path(target)
            error = verify_delete_target(path, runtime)
            if error:
                errors.append({"path": str(path), "error": error, "cache_key": candidate.cache_key})
                continue
            try:
                size = size_path(path)
                path.unlink()
                actions.append({"action": "deleted", "path": str(path), "size_bytes": size, "cache_key": candidate.cache_key})
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": str(path), "error": f"delete_failed:{type(exc).__name__}:{exc}", "cache_key": candidate.cache_key})
    return actions, errors


def build_report(runtime: Path, *, apply: bool, yes: bool, older_than_hours: int, max_rows: int) -> dict[str, Any]:
    protected, candidates, unknown, candidate_blockers, unknown_summary = classify_mosaics(runtime, older_than_hours=older_than_hours)
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if apply:
        if not yes:
            errors.append({"error": "apply_requires_yes", "message": "--apply requires --yes; no files were deleted"})
        elif candidate_blockers:
            errors.append({"error": "candidate_blocked_by_unknown_risk", "message": "Unknown-risk metadata references or may reference cleanup candidates; no files were deleted"})
        else:
            actions, errors = apply_candidates(candidates, runtime)
    return {
        "mode": "apply" if apply else "dry_run",
        "runtime_cache_dir": str(runtime),
        "summary": {
            "protected_mosaic_count": len(protected),
            "cleanup_candidate_count": len(candidates),
            "unknown_risk_count": len(unknown),
            "estimated_bytes_reclaimable": sum(item.size_bytes for item in candidates),
            "protection_reason_counts": reason_counts(protected),
        },
        "cleanup_candidates": [entry_dict(item) for item in candidates[:max_rows]],
        "protected_mosaics": [entry_dict(item) for item in protected[:max_rows]],
        "unknown_risk_items": unknown[:max_rows],
        "candidate_blockers": candidate_blockers[:max_rows],
        "unknown_risk_summary": unknown_summary,
        "retained_files": list(RETAINED_FILES),
        "delete_file_allowlist": list(COMPACTABLE_FILES),
        "forbidden_dirs": sorted(FORBIDDEN_DIRS),
        "actions_taken": actions,
        "errors": errors,
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
            value = row.get(key)
            if key.endswith("size_bytes"):
                value = human_size(int(value or 0))
            values.append(str(value if value is not None else ""))
        lines.append("| " + " | ".join(" ".join(value.splitlines()) for value in values) + " |")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any], max_rows: int) -> str:
    summary = report["summary"]
    nonblocking_unknown = [
        item
        for item in report["unknown_risk_items"]
        if not item.get("blocked_candidate_cache_keys")
    ]
    sections = [
        "# Wayback Mosaic Compaction Report",
        f"## Mode\n\n`{report['mode']}`",
        "## Summary\n\n"
        f"- Protected mosaics: {summary['protected_mosaic_count']}\n"
        f"- Cleanup candidates: {summary['cleanup_candidate_count']}\n"
        f"- Unknown-risk items: {summary['unknown_risk_count']}\n"
        f"- Estimated reclaimable: {human_size(summary['estimated_bytes_reclaimable'])}",
        "## Cleanup Candidates\n\n"
        + table(report["cleanup_candidates"], [("Cache key", "cache_key"), ("Size", "size_bytes"), ("Delete targets", "delete_targets")], max_rows),
        "## Protected Mosaics\n\n"
        + table(report["protected_mosaics"], [("Cache key", "cache_key"), ("Size", "size_bytes"), ("Reasons", "reasons"), ("Canonical COG", "canonical_cog_path")], max_rows),
        "## Unknown-Risk Metadata Scan\n\n"
        + table(
            report["unknown_risk_items"],
            [
                ("Path", "path"),
                ("Size", "size_bytes"),
                ("Classification", "classification"),
                ("Matched keys", "matched_cache_keys"),
                ("Blocked candidates", "blocked_candidate_cache_keys"),
                ("Reason", "reason"),
            ],
            max_rows,
        ),
        "## Candidate-Specific Blockers\n\n"
        + table(report["candidate_blockers"], [("Cache key", "cache_key"), ("Blockers", "blockers")], max_rows),
        "## Unknown-Risk Items Not Blocking Candidates\n\n"
        + table(
            nonblocking_unknown,
            [
                ("Path", "path"),
                ("Classification", "classification"),
                ("Matched keys", "matched_cache_keys"),
                ("Reason", "reason"),
            ],
            max_rows,
        ),
        "## Actions Taken\n\n" + table(report["actions_taken"], [("Action", "action"), ("Path", "path"), ("Size", "size_bytes")], max_rows),
        "## Errors\n\n" + table(report["errors"], [("Path", "path"), ("Error", "error"), ("Message", "message")], max_rows),
        "## Safety Rules\n\n"
        "- Dry-run is default.\n"
        "- Apply requires `--apply --yes`.\n"
        "- Only `mosaic.tif` and `mosaic.png` are eligible.\n"
        "- `metadata.json` and `valid_mask.tif` are retained.\n"
        "- Forbidden runtime cache areas are never targeted.",
    ]
    return "\n\n".join(sections) + "\n"


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely compact proven-redundant Wayback mosaic build artifacts.")
    parser.add_argument("--runtime-cache-dir")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run mode. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Delete compactable mosaic artifacts. Requires --yes.")
    parser.add_argument("--yes", action="store_true", help="Confirm apply mode deletion.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--older-than-hours", type=int, default=DEFAULT_OLDER_THAN_HOURS)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = build_report(
        runtime_cache_dir(args.runtime_cache_dir),
        apply=bool(args.apply),
        yes=bool(args.yes),
        older_than_hours=args.older_than_hours,
        max_rows=args.max_rows,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report, args.max_rows))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
