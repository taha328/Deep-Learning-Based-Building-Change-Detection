#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

docker compose --env-file .env exec -T backend-api /app/backend/.venv/bin/python - <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 900

payload = {
    "aoi_geojson": {
        "type": "Polygon",
        "coordinates": [[
            [-7.0, 33.0],
            [-6.9975, 33.0],
            [-6.9975, 33.0025],
            [-7.0, 33.0025],
            [-7.0, 33.0],
        ]],
    },
    "t1_release": "WB_2026_R04",
    "t2_release": "WB_2026_R05",
    "mode": "fast_preview",
    "latest_source": "esri_wayback",
    "inference_backend": "bandon_mps",
}


def request_json(method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(path: str) -> bytes:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=60) as response:
        return response.read()


print("Validating smoke AOI...")
validation = request_json("POST", "/api/detection/validate", payload)
if not validation.get("valid"):
    print(json.dumps(validation, indent=2), file=sys.stderr)
    raise SystemExit("Smoke AOI validation failed.")

print("Submitting async detection job...")
job_start = request_json("POST", "/api/jobs/detection", payload)
job_id = job_start["job_id"]
print(f"job_id={job_id}")

deadline = time.time() + TIMEOUT_SECONDS
job = {}
while time.time() < deadline:
    job = request_json("GET", f"/api/jobs/{urllib.parse.quote(job_id)}")
    status = job.get("status")
    print(f"status={status} progress={job.get('progress')} stage={job.get('stage')}")
    if status in {"completed", "failed", "cancelled"}:
        break
    time.sleep(5)
else:
    raise SystemExit("Smoke job timed out.")

if job.get("status") != "completed":
    print(json.dumps(job, indent=2), file=sys.stderr)
    raise SystemExit("Smoke job did not complete successfully.")

request_hash = job.get("request_hash") or job.get("result_run_id")
raw_result = job.get("raw_result") if isinstance(job.get("raw_result"), dict) else {}
if not request_hash:
    request_hash = raw_result.get("request_hash")
if not request_hash:
    raise SystemExit("Smoke job completed without request_hash/result_run_id.")

result = request_json("GET", f"/api/cache/runs/{urllib.parse.quote(request_hash)}")
if result.get("success") is not True:
    print(json.dumps(result, indent=2), file=sys.stderr)
    raise SystemExit("Cached smoke result was not successful.")

bandon = (((result.get("diagnostics") or {}).get("backend") or {}).get("bandon") or {})
device_resolved = bandon.get("device_resolved")
if device_resolved != "cpu":
    print(json.dumps(bandon, indent=2), file=sys.stderr)
    raise SystemExit(f"Expected device_resolved=cpu, got {device_resolved!r}.")

artifacts = result.get("artifacts") or []
if not artifacts:
    raise SystemExit("Smoke result did not include artifacts.")

png_path = None
for artifact in artifacts:
    path = artifact.get("path")
    media_type = artifact.get("media_type", "")
    if isinstance(path, str) and (path.endswith(".png") or media_type == "image/png"):
        png_path = path
        break

if not png_path:
    preview = result.get("preview_images") or {}
    for value in preview.values():
        if isinstance(value, str) and value.endswith(".png"):
            png_path = value
            break

if not png_path:
    raise SystemExit("No PNG artifact or preview path was available for /api/files retrieval.")

encoded_path = urllib.parse.quote(png_path, safe="")
content = fetch_bytes(f"/api/files?path={encoded_path}")
if not content:
    raise SystemExit("Retrieved PNG artifact was empty.")

print("Smoke test passed.")
print(f"request_hash={request_hash}")
print(f"device_resolved={device_resolved}")
print(f"artifact_count={len(artifacts)}")
print(f"retrieved_png={png_path}")
PY
