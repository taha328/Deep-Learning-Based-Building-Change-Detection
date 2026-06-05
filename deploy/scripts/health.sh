#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

FRONTEND_PORT="${FRONTEND_PORT:-8080}"
BASE_URL="http://127.0.0.1:${FRONTEND_PORT}"

check_url() {
  local label="$1"
  local url="$2"
  echo "Checking ${label}: ${url}"
  curl --fail --silent --show-error --max-time 15 "$url" >/dev/null
}

check_url "frontend" "${BASE_URL}/"
check_url "backend health" "${BASE_URL}/api/health"
check_url "database health" "${BASE_URL}/api/health/db"
check_url "redis health" "${BASE_URL}/api/health/redis"
check_url "backend registry" "${BASE_URL}/api/backends"

echo "Health checks passed."
