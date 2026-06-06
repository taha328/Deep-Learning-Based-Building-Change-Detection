# Client Handoff Checklist

## Release Preparation

- Verify checkpoint redistribution rights, license, and provenance.
- Generate the artifact with `./scripts/package-model-artifact.sh`.
- Verify the generated outer ZIP SHA256 and inspect the manifest.
- Deliver the ZIP and matching `.sha256` through an approved channel.
- Confirm no `.pth`, model ZIP, token, or credential is tracked or staged.

## Client Installation

```bash
git clone https://github.com/taha328/building_change_app.git
cd building_change_app/deploy
cp .env.example .env

MODEL_ARTIFACT_FILE=/path/to/building-change-model-bandon-mtgcdnet-v0.1.0.zip ./scripts/fetch-model.sh

./scripts/start.sh
./scripts/health.sh
./scripts/validate-runtime.sh
./scripts/smoke-test.sh
```

The installed checkpoint must exist at
`models/bandon/mtgcdnet_iter_40000.pth`. Docker mounts it read-only at
`/models/bandon/mtgcdnet_iter_40000.pth`.

## Acceptance Evidence

- `fetch-model.sh` reports checkpoint SHA256 verification passed.
- `start.sh` accepts the installed checkpoint and starts the stack.
- Health checks pass for frontend, backend, PostgreSQL, and Redis.
- Runtime validation reports `MODEL_DEVICE=auto` resolved to `cpu`.
- Smoke inference completes and retrieves a non-empty artifact.
- Checkpoint remains external to Git and Docker images.

## Delivery Caveats

- CPU Docker is the supported client deployment.
- CUDA remains optional, pending target NVIDIA machine validation, and is not production-certified.
- Never store model download credentials in `.env` or deployment files.
