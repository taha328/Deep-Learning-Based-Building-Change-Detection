#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$DEPLOY_DIR/scripts/stop.sh"
"$DEPLOY_DIR/scripts/start.sh"
