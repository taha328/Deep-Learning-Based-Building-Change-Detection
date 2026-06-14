#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


REQUIRED = {
    "docker-compose.yml",
    ".env",
    "scripts/start.sh",
    "scripts/health.sh",
    "scripts/stop.sh",
    "models/bandon/mtgcdnet_iter_40000.pth",
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "runtime_cache",
}
LOCAL_PATH_PATTERN = re.compile(r"/Users/|/home/|[A-Za-z]:\\\\Users\\\\")
SECRET_PATTERN = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA|OPENSSH|EC) PRIVATE KEY-----)"
)
MAPBOX_TOKEN_PATTERN = re.compile(r"^MAPBOX_API_KEY=(.*)$", re.MULTILINE)
SMOKE_TEST_PATHS = ("scripts/smoke-test.sh", "scripts/windows/smoke-test.ps1")


def normalized_names(names: list[str]) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        path = PurePosixPath(name)
        normalized.add(str(path))
        if len(path.parts) > 1:
            normalized.add(str(PurePosixPath(*path.parts[1:])))
    return normalized


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: verify-release-bundle.py path/to/building-change-app.zip")

    zip_path = Path(sys.argv[1])
    if not zip_path.is_file():
        raise SystemExit(f"Release bundle not found: {zip_path}")

    with ZipFile(zip_path) as archive:
        file_names = [name for name in archive.namelist() if not name.endswith("/")]
        normalized = normalized_names(file_names)
        missing = sorted(REQUIRED - normalized)
        if missing:
            raise SystemExit(f"Missing required release files: {missing}")

        duplicate_required = []
        for required in REQUIRED:
            count = sum(
                name == required or name.endswith(f"/{required}")
                for name in file_names
            )
            if count != 1:
                duplicate_required.append(f"{required} ({count})")
        if duplicate_required:
            raise SystemExit(f"Required release files must occur exactly once: {duplicate_required}")

        forbidden = sorted(
            name
            for name in file_names
            if FORBIDDEN_PARTS.intersection(PurePosixPath(name).parts)
        )
        if forbidden:
            raise SystemExit(f"Forbidden generated/source paths in release bundle: {forbidden}")

        for info in archive.infolist():
            if info.is_dir() or info.file_size > 10 * 1024 * 1024:
                continue
            payload = archive.read(info).decode("utf-8", errors="ignore")
            if LOCAL_PATH_PATTERN.search(payload):
                raise SystemExit(f"Local absolute path found in release file: {info.filename}")
            if SECRET_PATTERN.search(payload):
                raise SystemExit(f"Potential secret found in release file: {info.filename}")
            for match in MAPBOX_TOKEN_PATTERN.finditer(payload):
                token = match.group(1).strip()
                if token and not token.startswith("pk."):
                    raise SystemExit(f"Non-public Mapbox token found in release file: {info.filename}")

        checkpoint_names = [
            name
            for name in file_names
            if name == "models/bandon/mtgcdnet_iter_40000.pth"
            or name.endswith("/models/bandon/mtgcdnet_iter_40000.pth")
        ]
        checkpoint_info = archive.getinfo(checkpoint_names[0])
        if checkpoint_info.file_size == 0:
            raise SystemExit("Packaged checkpoint is empty.")

        for smoke_test_path in SMOKE_TEST_PATHS:
            matches = [
                name
                for name in file_names
                if name == smoke_test_path or name.endswith(f"/{smoke_test_path}")
            ]
            if len(matches) != 1:
                raise SystemExit(f"Release smoke test must occur exactly once: {smoke_test_path} ({len(matches)})")
            payload = archive.read(matches[0]).decode("utf-8", errors="ignore")
            if "latest_source" in payload:
                raise SystemExit(f"Removed latest_source field found in release smoke test: {smoke_test_path}")

    print(f"release bundle verification: OK ({zip_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
