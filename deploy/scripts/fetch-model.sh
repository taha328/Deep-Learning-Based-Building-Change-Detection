#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKPOINT_NAME="mtgcdnet_iter_40000.pth"
FINAL_DIR="$DEPLOY_DIR/models/bandon"
FINAL_PATH="$FINAL_DIR/$CHECKPOINT_NAME"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "Required checksum command not found: install sha256sum or shasum." >&2
    exit 1
  fi
}

require_command unzip
require_command awk

if [ -n "${MODEL_ARTIFACT_FILE:-}" ] && [ -n "${MODEL_ARTIFACT_URL:-}" ]; then
  echo "Set only one of MODEL_ARTIFACT_FILE or MODEL_ARTIFACT_URL." >&2
  exit 1
fi

if [ -z "${MODEL_ARTIFACT_FILE:-}" ] && [ -z "${MODEL_ARTIFACT_URL:-}" ]; then
  cat >&2 <<'EOF'
No model artifact source was provided.

Install from a local artifact:
  MODEL_ARTIFACT_FILE=/path/to/building-change-model-bandon-mtgcdnet-v0.1.1.zip ./scripts/fetch-model.sh

Or install from a controlled download URL:
  MODEL_ARTIFACT_URL=https://example/model-artifact.zip ./scripts/fetch-model.sh
EOF
  exit 1
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/building-change-fetch-model.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT
ARTIFACT_ZIP="$TEMP_DIR/model-artifact.zip"
EXTRACT_DIR="$TEMP_DIR/extracted"
mkdir -p "$EXTRACT_DIR"

if [ -n "${MODEL_ARTIFACT_FILE:-}" ]; then
  if [ ! -f "$MODEL_ARTIFACT_FILE" ]; then
    echo "Model artifact file not found: $MODEL_ARTIFACT_FILE" >&2
    exit 1
  fi
  cp "$MODEL_ARTIFACT_FILE" "$ARTIFACT_ZIP"
else
  require_command curl
  curl_args=(
    --fail
    --location
    --silent
    --show-error
    --header "Accept: application/octet-stream"
    --output "$ARTIFACT_ZIP"
  )
  if [ -n "${MODEL_ARTIFACT_AUTH_HEADER:-}" ]; then
    curl_args+=(--header "$MODEL_ARTIFACT_AUTH_HEADER")
  fi
  curl "${curl_args[@]}" "$MODEL_ARTIFACT_URL"
fi

unzip -q "$ARTIFACT_ZIP" -d "$EXTRACT_DIR"

CHECKSUM_FILE="$(find "$EXTRACT_DIR" -type f -name SHA256SUMS.txt -print -quit)"
CHECKPOINT_PATH="$(find "$EXTRACT_DIR" -type f -path "*/models/bandon/$CHECKPOINT_NAME" -print -quit)"

if [ -z "$CHECKPOINT_PATH" ]; then
  echo "Artifact does not contain models/bandon/$CHECKPOINT_NAME." >&2
  exit 1
fi

if [ -n "$CHECKSUM_FILE" ]; then
  EXPECTED_SHA="$(awk -v file="models/bandon/$CHECKPOINT_NAME" '$2 == file {print $1; exit}' "$CHECKSUM_FILE")"
  if [ -z "$EXPECTED_SHA" ]; then
    echo "SHA256SUMS.txt does not contain models/bandon/$CHECKPOINT_NAME." >&2
    exit 1
  fi
  ACTUAL_SHA="$(sha256_file "$CHECKPOINT_PATH")"
  if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
    echo "Checkpoint SHA256 verification failed." >&2
    exit 1
  fi
  echo "Checkpoint SHA256 verification passed."
else
  echo "Warning: artifact does not contain SHA256SUMS.txt; checkpoint checksum was not verified." >&2
fi

mkdir -p "$FINAL_DIR"
TEMP_FINAL="$FINAL_DIR/.$CHECKPOINT_NAME.tmp.$$"
cp "$CHECKPOINT_PATH" "$TEMP_FINAL"
mv -f "$TEMP_FINAL" "$FINAL_PATH"

if [ ! -f "$FINAL_PATH" ]; then
  echo "Checkpoint installation failed: $FINAL_PATH was not created." >&2
  exit 1
fi

echo "Model checkpoint installed."
echo "Path: $FINAL_PATH"
echo "Size: $(wc -c < "$FINAL_PATH" | tr -d ' ') bytes"
