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
from src.services.request_cleanup import audit_request_deletion_safety  # noqa: E402


def _request_hashes_for_project(settings: Settings, project_id: str) -> list[str]:
    path = settings.temporal_projects_dir / project_id / "project.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    hashes: list[str] = []
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict) and milestone.get("pair_request_hash"):
            hashes.append(str(milestone["pair_request_hash"]))
    return sorted(set(hashes))


def build_report(
    *,
    runtime_cache_dir: Path,
    request_hash: str | None = None,
    project_id: str | None = None,
    max_rows: int | None = None,
) -> dict[str, object]:
    settings = Settings(runtime_cache_dir=runtime_cache_dir)
    request_hashes = [request_hash] if request_hash else []
    if not request_hashes and project_id:
        request_hashes = _request_hashes_for_project(settings, project_id)
    if not request_hashes:
        request_hashes = sorted(path.name for path in settings.request_cache_dir.iterdir() if path.is_dir())
    reports = [
        audit_request_deletion_safety(request_hash=item, settings=settings, project_id=project_id).model_dump()
        for item in request_hashes
    ]
    if max_rows is not None and max_rows >= 0:
        reports = reports[:max_rows]
    return {
        "runtime_cache_dir": str(settings.runtime_cache_dir),
        "request_count": len(reports),
        "reports": reports,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-cache-dir", type=Path, default=Path("runtime_cache"))
    parser.add_argument("--request-hash")
    parser.add_argument("--project-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-rows", type=int)
    args = parser.parse_args(argv)

    report = build_report(
        runtime_cache_dir=args.runtime_cache_dir,
        request_hash=args.request_hash,
        project_id=args.project_id,
        max_rows=args.max_rows,
    )
    if args.request_hash and len(report["reports"]) == 1:
        payload = report["reports"][0]
    else:
        payload = report
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
