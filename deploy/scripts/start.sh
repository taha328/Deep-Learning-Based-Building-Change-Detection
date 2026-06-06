#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

CHECKPOINT="models/bandon/mtgcdnet_iter_40000.pth"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Desktop or Docker Engine first." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is required. Install a Docker version with 'docker compose' support." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi

if [ ! -f "$CHECKPOINT" ]; then
  echo "Missing model checkpoint: $DEPLOY_DIR/$CHECKPOINT" >&2
  echo >&2
  echo "Install it with:" >&2
  echo "  MODEL_ARTIFACT_FILE=/path/to/building-change-model-bandon-mtgcdnet-v0.1.0.zip ./scripts/fetch-model.sh" >&2
  echo >&2
  echo "or download the model artifact and place:" >&2
  echo "  models/bandon/mtgcdnet_iter_40000.pth" >&2
  exit 1
fi

echo "Pulling images if they are available from a registry..."
docker compose --env-file .env pull || echo "Image pull did not complete; continuing with locally available images."

echo "Starting database and Redis..."
docker compose --env-file .env up -d postgres redis

echo "Waiting for postgres and redis health checks..."
for _ in $(seq 1 60); do
  postgres_status="$(docker compose --env-file .env ps --format json postgres 2>/dev/null | grep -o '"Health":"[^"]*"' | head -1 | cut -d: -f2 | tr -d '"')" || true
  redis_status="$(docker compose --env-file .env ps --format json redis 2>/dev/null | grep -o '"Health":"[^"]*"' | head -1 | cut -d: -f2 | tr -d '"')" || true
  if [ "$postgres_status" = "healthy" ] && [ "$redis_status" = "healthy" ]; then
    break
  fi
  sleep 2
done

echo "Verifying PostgreSQL accepts application connections..."
database_ready=0
for _ in $(seq 1 60); do
  if docker compose --env-file .env run --rm --no-deps backend-api /app/backend/.venv/bin/python - <<'PY' >/dev/null 2>&1
import os
import psycopg
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
with psycopg.connect(
    host=url.host,
    port=url.port,
    user=url.username,
    password=url.password,
    dbname=url.database,
    connect_timeout=5,
):
    pass
PY
  then
    database_ready=1
    break
  fi
  sleep 2
done
if [ "$database_ready" -ne 1 ]; then
  echo "PostgreSQL did not accept application connections before timeout." >&2
  exit 1
fi

echo "Running PostgreSQL/PostGIS migration and verification..."
docker compose --env-file .env run --rm backend-api \
  /app/backend/.venv/bin/python /app/backend/scripts/setup_postgis_db.py --migrate --verify

echo "Starting application services..."
docker compose --env-file .env up -d backend-api celery-worker frontend

FRONTEND_PORT="$(grep -E '^FRONTEND_PORT=' .env | tail -1 | cut -d= -f2-)"
BACKEND_PORT="$(grep -E '^BACKEND_PORT=' .env | tail -1 | cut -d= -f2-)"
FRONTEND_PORT="${FRONTEND_PORT:-8080}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

echo
echo "Building Change Detection is starting."
echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}"
echo "Backend diagnostics: http://127.0.0.1:${BACKEND_PORT}/api/health"
echo
echo "Next checks:"
echo "  ./scripts/health.sh"
echo "  ./scripts/validate-runtime.sh"
echo "  ./scripts/smoke-test.sh"
