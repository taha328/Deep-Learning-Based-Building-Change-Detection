#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

if [ "$#" -gt 0 ]; then
  docker compose --env-file .env logs --tail=200 "$@"
else
  docker compose --env-file .env logs --tail=200
fi
