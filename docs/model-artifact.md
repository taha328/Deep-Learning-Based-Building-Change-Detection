# BANDON Model Artifact

The BANDON checkpoint is a separately delivered model artifact. It is not
tracked by Git, included in the client deployment bundle, baked into Docker
images, or published to GHCR.

## Artifact Contents

The versioned artifact is named:

```text
building-change-model-bandon-mtgcdnet-v0.1.1.zip
```

It contains the checkpoint at `models/bandon/mtgcdnet_iter_40000.pth`, an
internal `SHA256SUMS.txt`, a model card, and a redistribution/provenance notice.

## Generate Locally

From the source repository:

```bash
VERSION=v0.1.1 ./scripts/package-model-artifact.sh
```

The script reads the local vendor checkpoint and creates the ZIP, its outer
SHA256 file, and a manifest under `release/`. These generated files are ignored
by Git and must not be committed.

Before external distribution, verify the checkpoint redistribution rights,
license, and provenance. The package notice does not grant a license.

## Install Into A Deployment

From the `deploy/` directory:

```bash
MODEL_ARTIFACT_FILE=/path/to/building-change-model-bandon-mtgcdnet-v0.1.1.zip ./scripts/fetch-model.sh
```

For a controlled download URL:

```bash
MODEL_ARTIFACT_URL=https://github.com/taha328/building_change_app/releases/download/v0.1.1/building-change-model-bandon-mtgcdnet-v0.1.1.zip ./scripts/fetch-model.sh
```

Private GitHub release assets require their authenticated API asset URL rather
than the browser-style `releases/download/...` URL. Discover the API URL with an
authorized GitHub client:

```bash
gh api repos/taha328/building_change_app/releases/tags/v0.1.1 \
  --jq '.assets[] | select(.name == "building-change-model-bandon-mtgcdnet-v0.1.1.zip") | .url'
```

Pass that URL through `MODEL_ARTIFACT_URL` and the authentication header through
`MODEL_ARTIFACT_AUTH_HEADER`. Never print it, commit it, or store it in `.env`.

Windows uses `.\scripts\windows\fetch-model.ps1` with
`$env:MODEL_ARTIFACT_FILE` or `$env:MODEL_ARTIFACT_URL`.

The installer verifies the included checkpoint checksum and writes:

```text
deploy/models/bandon/mtgcdnet_iter_40000.pth
```

Manual placement at that exact path is also supported.

## Verify The Outer ZIP Checksum

On Linux:

```bash
sha256sum -c building-change-model-bandon-mtgcdnet-v0.1.1.sha256
```

On macOS:

```bash
shasum -a 256 building-change-model-bandon-mtgcdnet-v0.1.1.zip
cat building-change-model-bandon-mtgcdnet-v0.1.1.sha256
```

## Runtime Compatibility

- CPU Docker is the supported client runtime.
- Native MPS remains a development runtime.
- CUDA is pending validation on a compatible NVIDIA host and is not production-certified.
