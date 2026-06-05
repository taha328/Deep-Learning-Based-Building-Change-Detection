#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

output="$(docker compose --env-file .env run --rm -e MODEL_DEVICE=auto backend-api \
  /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json)"
echo "$output"

if ! printf '%s' "$output" | grep -q '"device_resolved": "cpu"'; then
  echo "Expected MODEL_DEVICE=auto to resolve to cpu in the supported CPU deployment." >&2
  exit 1
fi

echo "Runtime validation passed."
