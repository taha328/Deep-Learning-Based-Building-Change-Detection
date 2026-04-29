import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import sys
from typing import Iterable

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import Settings, get_settings


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    size_bytes: int


def _path_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _is_older_than(path: Path, cutoff: datetime) -> bool:
    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return modified < cutoff


def collect_cleanup_candidates(
    settings: Settings,
    *,
    older_than_days: int,
    include_exports: bool,
    include_tmp: bool,
    include_old_auto_bundles: bool,
    include_wayback_cache: bool,
) -> list[CleanupCandidate]:
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    candidates: dict[Path, CleanupCandidate] = {}

    def add_candidate(path: Path) -> None:
        if not path.exists() or not _is_older_than(path, cutoff):
            return
        candidates[path] = CleanupCandidate(path=path, size_bytes=_path_size_bytes(path))

    if include_tmp and settings.tmp_cache_dir.exists():
        for path in settings.tmp_cache_dir.iterdir():
            add_candidate(path)

    if include_exports:
        for root in (settings.request_cache_dir, settings.temporal_projects_dir):
            if not root.exists():
                continue
            for path in root.rglob("export_bundle.zip"):
                add_candidate(path)
            for path in root.rglob("temporal_project_bundle.zip"):
                add_candidate(path)

    if include_old_auto_bundles:
        for path in settings.request_cache_dir.rglob("export_bundle.zip"):
            add_candidate(path)
        for path in settings.temporal_projects_dir.rglob("temporal_project_bundle.zip"):
            add_candidate(path)

    if include_wayback_cache and settings.wayback_mosaic_cache_dir.exists():
        for path in settings.wayback_mosaic_cache_dir.iterdir():
            add_candidate(path)

    return sorted(candidates.values(), key=lambda item: str(item.path))


def apply_cleanup(candidates: Iterable[CleanupCandidate], *, destructive: bool) -> tuple[int, int]:
    deleted = 0
    freed = 0
    if not destructive:
        return deleted, freed

    for candidate in candidates:
        path = candidate.path
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
        deleted += 1
        freed += candidate.size_bytes
    return deleted, freed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean controlled runtime-cache temp/export artifacts.")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without deleting anything.")
    parser.add_argument("--older-than-days", type=int, default=7, help="Only target files older than N days.")
    parser.add_argument("--include-exports", action="store_true", help="Include generated export bundles.")
    parser.add_argument("--include-tmp", action="store_true", help="Include runtime_cache/tmp entries.")
    parser.add_argument("--include-old-auto-bundles", action="store_true", help="Include old auto-generated run/project bundles.")
    parser.add_argument("--include-wayback-cache", action="store_true", help="Also include shared Wayback cache entries.")
    parser.add_argument("--yes", action="store_true", help="Actually delete matching files.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()

    candidates = collect_cleanup_candidates(
        settings,
        older_than_days=args.older_than_days,
        include_exports=args.include_exports,
        include_tmp=args.include_tmp,
        include_old_auto_bundles=args.include_old_auto_bundles,
        include_wayback_cache=args.include_wayback_cache,
    )
    bytes_matched = sum(item.size_bytes for item in candidates)

    destructive = args.yes and not args.dry_run
    deleted, bytes_freed = apply_cleanup(candidates, destructive=destructive)

    print(f"files matched: {len(candidates)}")
    print(f"bytes that would be freed: {bytes_matched}")
    print(f"files deleted: {deleted}")
    print(f"bytes freed: {bytes_freed}")
    for candidate in candidates:
        print(candidate.path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
