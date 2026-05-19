#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd -P)"
PID_DIR="$REPO_ROOT/backend/runtime_cache/dev_pids"
mkdir -p "$PID_DIR"

STARTER_PID=""
CLEANED_UP=false

cleanup() {
  local status="${1:-$?}"
  if "$CLEANED_UP"; then
    exit "$status"
  fi
  CLEANED_UP=true
  if [[ -n "$STARTER_PID" ]] && kill -0 "$STARTER_PID" >/dev/null 2>&1; then
    kill -TERM "$STARTER_PID" >/dev/null 2>&1 || true
    wait "$STARTER_PID" >/dev/null 2>&1 || true
  fi
  ./scripts/dev_stop_all.sh
  exit "$status"
}

trap 'cleanup $?' EXIT
trap 'cleanup 0' INT TERM

for port in 5173 8000; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[dev] port $port is occupied by:"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
    echo "[dev] cleaning existing dev stack before start"
  fi
done

./scripts/dev_stop_all.sh

remaining_celery="$(ps -axo pid=,command= | awk '/src\.jobs\.celery_app\.celery_app|building_change_worker/ && !/awk/ { print $1 }' | sort -u | tr '\n' ' ')"
if [[ -n "${remaining_celery// }" ]]; then
  echo "[dev] refusing to start: stale project-owned Celery worker(s) remain: $remaining_celery" >&2
  exit 1
fi

python3 scripts/dev_start_all.py &
STARTER_PID="$!"
wait "$STARTER_PID"
STARTER_PID=""
