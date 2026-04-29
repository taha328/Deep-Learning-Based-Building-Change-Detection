#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_import_path() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))


def main() -> int:
    os.environ.setdefault("PYTHONNOUSERSITE", "1")
    _ensure_import_path()

    from src.config import get_settings
    from src.jobs.service import reconcile_stale_jobs

    settings = get_settings()
    failed_count = reconcile_stale_jobs(settings)
    print(f"Marked {failed_count} stale job(s) as failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
