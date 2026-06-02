#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import Settings  # noqa: E402
from src.services.request_cleanup import cleanup_request_after_successful_promotion  # noqa: E402


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _project_checks(settings: Settings, project_id: str, request_hash: str) -> dict[str, object]:
    project_path = settings.temporal_projects_dir / project_id / "project.json"
    payload = _read_json(project_path)
    checks: dict[str, object] = {
        "project_api_still_loads": payload is not None,
        "frontend_required_metadata_present": False,
        "reference_imagery_present": False,
        "reference_tilejson_still_works": False,
        "temporal_vector_artifacts_exist": False,
        "qgis_candidate_discovery_works": False,
        "download_export_path_exists": False,
        "metadata_paths_pointing_to_request": [],
    }
    if payload is None:
        return checks

    vector_count = 0
    reference_count = 0
    for milestone in payload.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        ref = milestone.get("reference_imagery")
        if isinstance(ref, dict) and (ref.get("tilejson_url") or ref.get("tiles_url_template") or ref.get("cog_path")):
            reference_count += 1
            cog_path = ref.get("cog_path") or ref.get("canonical_cog_path")
            if isinstance(cog_path, str) and Path(cog_path).exists():
                checks["reference_tilejson_still_works"] = True
        for artifact in milestone.get("artifacts") or []:
            if isinstance(artifact, dict):
                path = artifact.get("path")
                if isinstance(path, str) and Path(path).exists():
                    vector_count += 1
    checks["reference_imagery_present"] = reference_count > 0
    checks["frontend_required_metadata_present"] = reference_count > 0 and bool(payload.get("milestones"))
    checks["temporal_vector_artifacts_exist"] = vector_count > 0
    checks["download_export_path_exists"] = bool(payload.get("download_bundle_path") and Path(str(payload["download_bundle_path"])).exists())

    for field_path, value in _walk_values(payload):
        if isinstance(value, str) and f"/requests/{request_hash}" in value:
            checks["metadata_paths_pointing_to_request"].append({"field": field_path, "path": value})  # type: ignore[index]
    try:
        sys.path.insert(0, str(BACKEND_ROOT.parent / "qgis_plugin"))
        from building_change_plugin.models import discover_temporal_layer_candidates

        checks["qgis_candidate_discovery_works"] = bool(discover_temporal_layer_candidates(payload))
    except Exception as exc:
        checks["qgis_candidate_discovery_works"] = False
        checks["qgis_error"] = str(exc)
    return checks


def _walk_values(value: object, *, path: str = "") -> list[tuple[str, object]]:
    found = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk_values(child, path=f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_values(child, path=f"{path}[{index}]"))
    return found


def verify_cleanup(
    *,
    runtime_cache_dir: Path,
    project_id: str,
    request_hash: str,
    mode: str,
    apply: bool,
    yes: bool,
) -> dict[str, object]:
    settings = Settings(runtime_cache_dir=runtime_cache_dir)
    if apply and not yes:
        return {"error": "apply_requires_yes", "mode": mode, "applied": False}
    release_identifier = None
    payload = _read_json(settings.temporal_projects_dir / project_id / "project.json")
    if payload:
        for milestone in payload.get("milestones") or []:
            if isinstance(milestone, dict) and milestone.get("pair_request_hash") == request_hash:
                release_identifier = milestone.get("release_identifier")
                break
    before = _project_checks(settings, project_id, request_hash)
    cleanup = cleanup_request_after_successful_promotion(
        request_hash=request_hash,
        project_id=project_id,
        release_identifier=str(release_identifier) if release_identifier else None,
        mode=mode,  # type: ignore[arg-type]
        settings=settings,
        dry_run=not apply,
    )
    after = _project_checks(settings, project_id, request_hash)
    return {
        "project_id": project_id,
        "request_hash": request_hash,
        "mode": mode,
        "applied": apply,
        "before_checks": before,
        "cleanup": cleanup.model_dump(),
        "after_checks": after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-cache-dir", type=Path, default=Path("runtime_cache"))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--request-hash", required=True)
    parser.add_argument("--mode", choices=("compact_heavy", "delete_full"), default="compact_heavy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = verify_cleanup(
        runtime_cache_dir=args.runtime_cache_dir,
        project_id=args.project_id,
        request_hash=args.request_hash,
        mode=args.mode,
        apply=args.apply,
        yes=args.yes,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
