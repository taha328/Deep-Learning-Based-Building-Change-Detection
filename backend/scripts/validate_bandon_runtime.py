from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import get_settings  # noqa: E402
from src.domain.bandon_runner import BandonRuntimeProbe, probe_bandon_runtime  # noqa: E402


def build_payload(probe: BandonRuntimeProbe) -> dict[str, Any]:
    return {
        "available": probe.available,
        "message": probe.message,
        "device_requested": probe.device_requested,
        "device_resolved": probe.device_resolved,
        "cuda_available": probe.cuda_available,
        "cuda_device_count": probe.cuda_device_count,
        "cuda_device_name": probe.cuda_device_name,
        "torch_cuda_version": probe.torch_cuda_version,
        "mps_available": probe.mps_available,
        "mps_built": probe.mps_built,
        "torch_version": probe.torch_version,
        "mmcv_version": probe.mmcv_version,
        "repo_dir": probe.repo_dir,
        "env_prefix": probe.env_prefix,
        "runner_path": probe.runner_path,
        "config_path": probe.config_path,
        "checkpoint_path": probe.checkpoint_path,
        "python_executable": probe.python_executable,
        "launcher": probe.launcher,
        "diagnostics": probe.diagnostics(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate local BANDON runtime readiness.")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Print compact JSON.")
    output_group.add_argument("--pretty", action="store_true", help="Print pretty JSON. This is the default.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    probe = probe_bandon_runtime(settings)
    payload = build_payload(probe)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if probe.available else 1


if __name__ == "__main__":
    raise SystemExit(main())
