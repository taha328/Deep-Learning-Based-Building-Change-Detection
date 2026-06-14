#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
DIST_DIR="$REPO_ROOT/dist"
BUNDLE_NAME="building-change-app"
ZIP_PATH="$DIST_DIR/$BUNDLE_NAME.zip"
CHECKPOINT_NAME="mtgcdnet_iter_40000.pth"
CHECKPOINT_PATH="${CHECKPOINT_PATH:-$REPO_ROOT/vendor/BANDON-mps/checkpoints/$CHECKPOINT_NAME}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

require_file() {
  if [ ! -f "$1" ]; then
    echo "Required release file not found: $1" >&2
    exit 1
  fi
}

require_command python3
require_command zip

require_file "$DEPLOY_DIR/docker-compose.yml"
require_file "$DEPLOY_DIR/.env.example"
require_file "$DEPLOY_DIR/scripts/start.sh"
require_file "$DEPLOY_DIR/scripts/health.sh"
require_file "$DEPLOY_DIR/scripts/stop.sh"
require_file "$CHECKPOINT_PATH"

STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/building-change-app-release.XXXXXX")"
trap 'rm -rf "$STAGING_ROOT"' EXIT

APP_ROOT="$STAGING_ROOT/$BUNDLE_NAME"
mkdir -p "$APP_ROOT/models/bandon"

cp "$DEPLOY_DIR/docker-compose.yml" "$APP_ROOT/docker-compose.yml"
cp "$DEPLOY_DIR/docker-compose.cuda.yml" "$APP_ROOT/docker-compose.cuda.yml"
cp "$DEPLOY_DIR/.env.example" "$APP_ROOT/.env"
cp "$DEPLOY_DIR/.env.example" "$APP_ROOT/.env.example"
cp -R "$DEPLOY_DIR/scripts" "$APP_ROOT/scripts"
cp "$CHECKPOINT_PATH" "$APP_ROOT/models/bandon/$CHECKPOINT_NAME"

find "$APP_ROOT/scripts" -type f -name '*.sh' -exec chmod 0755 {} +

mkdir -p "$DIST_DIR"
rm -f "$ZIP_PATH"
(
  cd "$STAGING_ROOT"
  zip -q -r "$ZIP_PATH" "$BUNDLE_NAME"
)

python3 "$REPO_ROOT/scripts/verify-release-bundle.py" "$ZIP_PATH"
echo "Release bundle generated: $ZIP_PATH"
