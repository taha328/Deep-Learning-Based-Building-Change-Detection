#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${BUILDING_CHANGE_REPOSITORY:-taha328/Deep-Learning-Based-Building-Change-Detection}"
ASSET_NAME="building-change-app.zip"
INSTALL_BASE="${BUILDING_CHANGE_INSTALL_BASE:-$HOME/.local/share/building-change-app/releases}"
INSTALL_ID="$(date -u +%Y%m%dT%H%M%SZ)"
DOWNLOAD_URL="${BUILDING_CHANGE_ASSET_URL:-https://github.com/$REPOSITORY/releases/latest/download/$ASSET_NAME}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

require_command curl
require_command docker
require_command unzip

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is required. Install a Docker version with 'docker compose' support." >&2
  exit 1
fi

mkdir -p "$INSTALL_BASE"
INSTALL_DIR="$(mktemp -d "$INSTALL_BASE/${INSTALL_ID}.XXXXXX")"
ZIP_PATH="$INSTALL_DIR/$ASSET_NAME"

cleanup_failed_install() {
  status=$?
  if [ "$status" -ne 0 ]; then
    rm -rf "$INSTALL_DIR"
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup_failed_install EXIT

echo "Downloading latest Building Change Detection release..."
curl --fail --silent --show-error --location "$DOWNLOAD_URL" --output "$ZIP_PATH"
unzip -q "$ZIP_PATH" -d "$INSTALL_DIR"
rm -f "$ZIP_PATH"

COMPOSE_PATH="$(find "$INSTALL_DIR" -maxdepth 2 -name docker-compose.yml -type f -print -quit)"
if [ -z "$COMPOSE_PATH" ]; then
  echo "Downloaded release bundle does not contain docker-compose.yml." >&2
  exit 1
fi
APP_DIR="$(dirname "$COMPOSE_PATH")"
if [ ! -f "$APP_DIR/.env" ]; then
  echo "Downloaded release bundle is incomplete." >&2
  exit 1
fi

cd "$APP_DIR"
if [ "${BUILDING_CHANGE_SKIP_START:-0}" = "1" ]; then
  docker compose --env-file .env config >/dev/null
  trap - EXIT
  echo "Bundle installation validation passed: $APP_DIR"
  exit 0
fi

./scripts/start.sh
./scripts/health.sh

FRONTEND_PORT="$(grep -E '^FRONTEND_PORT=' .env | tail -1 | cut -d= -f2-)"
FRONTEND_PORT="${FRONTEND_PORT:-8080}"

trap - EXIT
echo
echo "Installation directory: $APP_DIR"
echo "Application: http://127.0.0.1:${FRONTEND_PORT}"
echo "Stop the application with: $APP_DIR/scripts/stop.sh"
