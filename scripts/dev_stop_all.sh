#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd -P)"
PID_DIR="$REPO_ROOT/backend/runtime_cache/dev_pids"
FLUSH_REDIS=false
STOP_REDIS=false

for arg in "$@"; do
  case "$arg" in
    --flush-redis)
      FLUSH_REDIS=true
      ;;
    --stop-redis)
      STOP_REDIS=true
      ;;
    *)
      echo "[dev-stop] unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$PID_DIR"

is_running() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1
}

process_command() {
  local pid="$1"
  ps -p "$pid" -o command= 2>/dev/null || true
}

pid_belongs_to_service() {
  local service="$1"
  local pid="$2"
  local command
  command="$(process_command "$pid")"
  case "$service" in
    backend)
      [[ "$command" == *"$REPO_ROOT/backend"* || "$command" == *"scripts/start_backend.py"* || "$command" == *"uvicorn src.api.main:app"* ]]
      ;;
    celery)
      [[ "$command" == *"$REPO_ROOT/backend"* || "$command" == *"src.jobs.celery_app.celery_app"* || "$command" == *"building_change_worker"* ]]
      ;;
    frontend)
      [[ "$command" == *"$REPO_ROOT/frontend"* || "$command" == *"vite"* || "$command" == *"npm run dev"* ]]
      ;;
    redis)
      [[ "$command" == *"redis-server"* ]]
      ;;
    *)
      return 1
      ;;
  esac
}

terminate_pid() {
  local service="$1"
  local pid="$2"
  if ! is_running "$pid"; then
    return
  fi
  if ! pid_belongs_to_service "$service" "$pid"; then
    echo "[dev-stop] skip service=$service pid=$pid reason=pid_does_not_match_project command=$(process_command "$pid")"
    return
  fi

  echo "[dev-stop] SIGTERM service=$service pid=$pid"
  kill -TERM "-$pid" >/dev/null 2>&1 || true
  kill -TERM "$pid" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    if ! is_running "$pid"; then
      return
    fi
    sleep 0.5
  done
  if is_running "$pid"; then
    echo "[dev-stop] SIGKILL service=$service pid=$pid"
    kill -KILL "-$pid" >/dev/null 2>&1 || true
    kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
}

stop_from_pid_file() {
  local service="$1"
  local file="$PID_DIR/$service.pid"
  [[ -f "$file" ]] || return 0
  local pid
  pid="$(tr -d '[:space:]' < "$file" || true)"
  if [[ -n "$pid" ]]; then
    terminate_pid "$service" "$pid"
  fi
  rm -f "$file"
}

fallback_pids_for_service() {
  local service="$1"
  case "$service" in
    backend)
      ps -axo pid=,command= | awk '
        /scripts\/start_backend.py|uvicorn src\.api\.main:app/ && !/awk/ { print $1 }
      '
      ;;
    celery)
      ps -axo pid=,command= | awk '
        /src\.jobs\.celery_app\.celery_app|building_change_worker/ && !/awk/ { print $1 }
      '
      ;;
    frontend)
      ps -axo pid=,command= | awk -v root="$REPO_ROOT" '
        ($0 ~ root && ($0 ~ /npm run dev/ || $0 ~ /vite/)) && !/awk/ { print $1 }
      '
      ;;
    *)
      return
      ;;
  esac
}

stop_fallback_service() {
  local service="$1"
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    terminate_pid "$service" "$pid"
  done < <(fallback_pids_for_service "$service" | sort -u)
}

remaining_count() {
  local service="$1"
  fallback_pids_for_service "$service" | sort -u | wc -l | tr -d '[:space:]'
}

stop_from_pid_file "frontend"
stop_from_pid_file "backend"
stop_from_pid_file "celery"
stop_from_pid_file "redis"

if "$STOP_REDIS" && command -v redis-cli >/dev/null 2>&1 && redis-cli ping >/dev/null 2>&1; then
  redis_pid="$(redis-cli info server 2>/dev/null | awk -F: '/^process_id:/ { gsub(/\r/, "", $2); print $2 }' | head -1)"
  if [[ -n "${redis_pid:-}" ]]; then
    terminate_pid "redis" "$redis_pid"
  fi
fi

stop_fallback_service "frontend"
stop_fallback_service "backend"
stop_fallback_service "celery"

if "$FLUSH_REDIS"; then
  echo "LOCAL DEV ONLY: this clears queued Celery jobs and task results."
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli flushdb
  else
    echo "[dev-stop] redis-cli not found; cannot flush Redis" >&2
  fi
fi

echo "DEV_PROCESS_VERIFY service=backend remaining=$(remaining_count backend)"
echo "DEV_PROCESS_VERIFY service=frontend remaining=$(remaining_count frontend)"
echo "DEV_PROCESS_VERIFY service=celery remaining=$(remaining_count celery)"
if command -v redis-cli >/dev/null 2>&1 && redis-cli ping >/dev/null 2>&1; then
  if [[ -f "$PID_DIR/redis.pid" ]]; then
    echo "DEV_PROCESS_VERIFY service=redis remaining=1 external=false"
  else
    echo "DEV_PROCESS_VERIFY service=redis remaining=1 external=true"
  fi
else
  echo "DEV_PROCESS_VERIFY service=redis remaining=0 external=false"
fi
