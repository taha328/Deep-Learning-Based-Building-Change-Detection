#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for port in 5173 8000; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[dev] port $port is occupied by:"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
    echo "[dev] cleaning existing dev stack before start"
  fi
done

./scripts/dev_stop_all.sh
python3 scripts/dev_start_all.py
