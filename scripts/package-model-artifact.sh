#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-v0.1.0}"
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must use semantic release format such as v0.1.1." >&2
  exit 1
fi

ARTIFACT_NAME="building-change-model-bandon-mtgcdnet-$VERSION"
CHECKPOINT_NAME="mtgcdnet_iter_40000.pth"
SOURCE_CHECKPOINT="$REPO_ROOT/vendor/BANDON-mps/checkpoints/$CHECKPOINT_NAME"
RELEASE_DIR="$REPO_ROOT/release"
ZIP_PATH="$RELEASE_DIR/$ARTIFACT_NAME.zip"
ZIP_SHA_PATH="$RELEASE_DIR/$ARTIFACT_NAME.sha256"
MANIFEST_PATH="$RELEASE_DIR/$ARTIFACT_NAME.MANIFEST.txt"

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

require_command zip
require_command unzip
require_command awk

if [ ! -f "$SOURCE_CHECKPOINT" ]; then
  echo "Source checkpoint not found: $SOURCE_CHECKPOINT" >&2
  exit 1
fi

mkdir -p "$RELEASE_DIR"
STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/building-change-model-artifact.XXXXXX")"
trap 'rm -rf "$STAGING_ROOT"' EXIT

ARTIFACT_ROOT="$STAGING_ROOT/$ARTIFACT_NAME"
CHECKPOINT_DIR="$ARTIFACT_ROOT/models/bandon"
mkdir -p "$CHECKPOINT_DIR"
cp "$SOURCE_CHECKPOINT" "$CHECKPOINT_DIR/$CHECKPOINT_NAME"

CHECKPOINT_SHA="$(sha256_file "$CHECKPOINT_DIR/$CHECKPOINT_NAME")"
printf '%s  %s\n' "$CHECKPOINT_SHA" "models/bandon/$CHECKPOINT_NAME" > "$ARTIFACT_ROOT/SHA256SUMS.txt"

cat > "$ARTIFACT_ROOT/MODEL_CARD.md" <<EOF
# BANDON MTGCDNet Model Artifact

- Model artifact: \`$ARTIFACT_NAME\`
- Version: \`$VERSION\`
- Checkpoint: \`$CHECKPOINT_NAME\`
- Expected deploy path: \`deploy/models/bandon/$CHECKPOINT_NAME\`
- Expected container path: \`/models/bandon/$CHECKPOINT_NAME\`
- Compatible backend image: \`ghcr.io/taha328/building-change-backend:cpu-v0.1.0\`
- Runtime: CPU Docker; native MPS development; CUDA pending NVIDIA validation
- Source/provenance: local \`vendor/BANDON-mps\` checkpoint, to be reviewed
- Checkpoint SHA256: \`$CHECKPOINT_SHA\`
EOF

cat > "$ARTIFACT_ROOT/LICENSE_NOTICE.md" <<'EOF'
# License And Redistribution Notice

Model checkpoint redistribution, license, and provenance must be verified before external distribution.

Do not distribute this artifact unless you have the right to distribute the checkpoint.

This notice does not grant a license and does not identify or replace the checkpoint's applicable license.
EOF

rm -f "$ZIP_PATH" "$ZIP_SHA_PATH" "$MANIFEST_PATH"
(
  cd "$STAGING_ROOT"
  zip -q -r "$ZIP_PATH" "$ARTIFACT_NAME"
)

ZIP_SHA="$(sha256_file "$ZIP_PATH")"
printf '%s  %s\n' "$ZIP_SHA" "$(basename "$ZIP_PATH")" > "$ZIP_SHA_PATH"

{
  echo "artifact_name=$ARTIFACT_NAME"
  echo "version=$VERSION"
  echo "zip=$(basename "$ZIP_PATH")"
  echo "zip_sha256=$ZIP_SHA"
  echo "checkpoint=models/bandon/$CHECKPOINT_NAME"
  echo "checkpoint_sha256=$CHECKPOINT_SHA"
  echo "zip_size_bytes=$(wc -c < "$ZIP_PATH" | tr -d ' ')"
  echo
  echo "ZIP contents:"
  unzip -l "$ZIP_PATH"
} > "$MANIFEST_PATH"

echo "Model artifact generated."
echo "ZIP: $ZIP_PATH"
echo "SHA256: $ZIP_SHA_PATH"
echo "Manifest: $MANIFEST_PATH"
