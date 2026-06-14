from __future__ import annotations

from pathlib import Path

from src.config import Settings


def canonical_runtime_path(
    settings: Settings,
    configured_path: str | Path | None,
    *,
    fallback_relative: str | Path,
) -> Path:
    """Resolve persisted paths inside the configured shared runtime cache."""
    runtime_root = settings.runtime_cache_dir.expanduser().resolve()
    fallback = (runtime_root / fallback_relative).resolve()
    if configured_path is None:
        return fallback

    candidate = Path(configured_path).expanduser()
    if not candidate.is_absolute():
        parts = candidate.parts
        if "runtime_cache" in parts:
            candidate = runtime_root.joinpath(*parts[parts.index("runtime_cache") + 1 :])
        else:
            candidate = runtime_root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(runtime_root)
    except ValueError:
        return fallback
    return candidate


def temporal_project_dir(settings: Settings, project_id: str, configured_path: str | Path | None = None) -> Path:
    del configured_path
    return (settings.temporal_projects_dir / project_id).expanduser().resolve()


def temporal_project_file_candidates(
    settings: Settings,
    project_id: str,
    configured_path: str | Path | None = None,
) -> list[Path]:
    canonical = temporal_project_dir(settings, project_id, configured_path) / "project.json"
    default = settings.temporal_projects_dir.resolve() / project_id / "project.json"
    return list(dict.fromkeys((canonical, default)))
