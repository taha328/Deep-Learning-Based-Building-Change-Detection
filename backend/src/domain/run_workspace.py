from __future__ import annotations

import shutil
from pathlib import Path

from src.config import Settings


def get_run_tmp_dir(settings: Settings, run_id: str) -> Path:
    path = settings.tmp_cache_dir / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_run_tmp_dir(settings: Settings, run_id: str, *, success: bool) -> None:
    path = settings.tmp_cache_dir / run_id
    if not path.exists():
        return
    if success and not settings.keep_intermediate_artifacts:
        shutil.rmtree(path, ignore_errors=True)
