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
    ".DS_Store",
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
PUBLIC_MAPBOX_TOKEN = "pk.eyJ1IjoidGFoYWVsIiwiYSI6ImNtbnl6dHdqcjA3Z3EycXNmZHQyM3FkZWQifQ.IDf_zeGoMaPHcrsLOD5q7A"
REQUIRED_ENV_VALUES = {
    "BACKEND_IMAGE": "ghcr.io/taha328/building-change-backend:cpu-v0.1.5",
    "FRONTEND_IMAGE": "ghcr.io/taha328/building-change-frontend:v0.1.5",
    "MAPBOX_API_KEY": PUBLIC_MAPBOX_TOKEN,
    "MAPBOX_ACCESS_TOKEN": PUBLIC_MAPBOX_TOKEN,
    "MODEL_DEVICE": "auto",
    "APP_INFERENCE_BACKEND": "bandon_mps",
    "APP_CHANGE_THRESHOLD": "0.50",
    "APP_SEMANTIC_THRESHOLD": "0.50",
    "APP_MAPBOX_MAX_TILES_PER_REQUEST": "1024",
    "MAPBOX_CURRENT_IMAGERY_MAX_TILES": "1024",
    "APP_WAYBACK_DEFAULT_ZOOM": "18",
    "APP_TILE_ZOOM": "18",
    "APP_WAYBACK_HTTP_CONNECT_TIMEOUT_SECONDS": "60",
    "APP_WAYBACK_HTTP_READ_TIMEOUT_SECONDS": "120",
    "APP_WAYBACK_HTTP_MAX_RETRIES": "8",
    "APP_WAYBACK_HTTP_BACKOFF_BASE_SECONDS": "1.0",
    "APP_WAYBACK_TILE_MAX_CONCURRENCY": "12",
    "APP_WAYBACK_MAX_MISSING_TILE_RATIO": "0.05",
    "APP_POST_COMPLETION_REQUEST_CLEANUP_ENABLED": "true",
    "APP_POST_COMPLETION_REQUEST_CLEANUP_MODE": "compact_heavy",
    "APP_POST_COMPLETION_REQUEST_CLEANUP_GRACE_SECONDS": "300",
    "APP_POST_COMPLETION_REQUEST_CLEANUP_KEEP_PROVENANCE": "true",
    "APP_POST_COMPLETION_REQUEST_CLEANUP_DELETE_EXPORT_BUNDLE": "true",
}
FORBIDDEN_ENV_KEYS = {
    "APP_S2LOOKING_CHECKPOINT_PATH",
}
REQUIRED_COMPOSE_ENV_KEYS = tuple(
    key
    for key in REQUIRED_ENV_VALUES
    if key
    not in {
        "BACKEND_IMAGE",
        "FRONTEND_IMAGE",
        "MAPBOX_API_KEY",
    }
) + ("MAPBOX_API_KEY",)


def normalized_names(names: list[str]) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        path = PurePosixPath(name)
        normalized.add(str(path))
        if len(path.parts) > 1:
            normalized.add(str(PurePosixPath(*path.parts[1:])))
    return normalized


def find_one(file_names: list[str], suffix: str) -> str:
    matches = [name for name in file_names if name == suffix or name.endswith(f"/{suffix}")]
    if len(matches) != 1:
        raise SystemExit(f"Release file must occur exactly once: {suffix} ({len(matches)})")
    return matches[0]


def parse_env(payload: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


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

        env_name = find_one(file_names, ".env")
        env_payload = archive.read(env_name).decode("utf-8", errors="ignore")
        env_values = parse_env(env_payload)
        for key, expected in REQUIRED_ENV_VALUES.items():
            actual = env_values.get(key)
            if actual != expected:
                raise SystemExit(f"Release .env has {key}={actual!r}; expected {expected!r}")
        forbidden_present = sorted(key for key in FORBIDDEN_ENV_KEYS if key in env_values)
        if forbidden_present:
            raise SystemExit(f"Forbidden local-only release .env keys found: {forbidden_present}")
        for key in ("MAPBOX_API_KEY", "MAPBOX_ACCESS_TOKEN"):
            token = env_values[key]
            if not token.startswith("pk."):
                raise SystemExit(f"{key} must be a public Mapbox token beginning with pk.")

        compose_name = find_one(file_names, "docker-compose.yml")
        compose_payload = archive.read(compose_name).decode("utf-8", errors="ignore")
        for key in REQUIRED_COMPOSE_ENV_KEYS:
            if key not in compose_payload:
                raise SystemExit(f"docker-compose.yml does not pass required release env key: {key}")

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
