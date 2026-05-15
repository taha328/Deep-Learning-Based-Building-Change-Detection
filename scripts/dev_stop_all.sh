#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

kill_pattern() {
  local pattern="$1"
  pkill -f "$pattern" >/dev/null 2>&1 || true
}

stop_component() {
  local label="$1"
  shift
  echo "[dev-stop] stopping $label"
  local pattern
  for pattern in "$@"; do
    kill_pattern "$pattern"
  done
}

kill_port() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      kill -9 "$pid" 2>/dev/null || true
    done <<< "$pids"
  fi

  if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[dev-stop] port $port still occupied"
  else
    echo "[dev-stop] port $port free"
  fi
}

stop_component "frontend" "vite.*Building_change_app" "node.*vite.*5173" "node.*dev"
stop_component "backend" "uvicorn src.api.main:app" "scripts/start_backend.py"
stop_component "celery" "celery.*src.jobs.celery_app" "celery.*building_change_worker"

echo "[dev-stop] verifying ports are free after kill:"
kill_port 5173
kill_port 5174
kill_port 8000
