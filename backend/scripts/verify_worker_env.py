from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.jobs.worker_runtime import collect_worker_diagnostics, format_worker_diagnostics, validate_worker_environment


def main() -> int:
    diagnostics = collect_worker_diagnostics()
    print(format_worker_diagnostics(diagnostics))
    validate_worker_environment(diagnostics)
    print("Worker environment check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
