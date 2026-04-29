from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

if os.environ.get("PYTHONNOUSERSITE") != "1":
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import get_settings
from src.jobs.worker_runtime import build_worker_command


def main() -> int:
    settings = get_settings()
    command = build_worker_command(settings)
    print("Starting Celery worker:", " ".join(shlex.quote(part) for part in command), flush=True)
    os.execvpe(command[0], command, os.environ.copy())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
