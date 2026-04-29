from __future__ import annotations

import argparse
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
from src.jobs.worker_runtime import build_backend_command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the Building Change FastAPI backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--no-reload", action="store_true", help="Disable Uvicorn reload mode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    command = build_backend_command(
        settings,
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
    )
    print("Starting FastAPI backend:", " ".join(shlex.quote(part) for part in command), flush=True)
    os.execvpe(command[0], command, os.environ.copy())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
