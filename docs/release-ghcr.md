# GHCR Image Release Guide

This project uses GitHub Container Registry for client deployment images.

## Images

CPU client delivery images:

```text
ghcr.io/taha328/building-change-backend:cpu-v0.1.0
ghcr.io/taha328/building-change-backend:cpu-latest
ghcr.io/taha328/building-change-frontend:v0.1.0
ghcr.io/taha328/building-change-frontend:latest
```

The CPU backend and frontend tags are published as multi-platform images for
`linux/amd64` and `linux/arm64`. Docker automatically pulls the correct variant
for the host architecture. These images support Docker hosts with sufficient RAM
and disk on those two architectures.

The client deployment uses `imresamu/postgis:16-3.4`, which publishes native
`linux/amd64` and `linux/arm64` variants while preserving PostgreSQL 16 and
PostGIS 3.4 compatibility. The image labels identify its source as the upstream
`postgis/docker-postgis` project. The selected image is configurable through
`POSTGIS_IMAGE` in the deployment environment.

Optional CUDA image:

```text
ghcr.io/taha328/building-change-backend:cuda-v0.1.0
```

CUDA remains optional, `linux/amd64`-oriented, and not production-certified until
NVIDIA-host validation passes.

## Publish With GitHub Actions

Workflow:

```text
.github/workflows/publish-images.yml
```

Manual publish:

1. Open GitHub Actions.
2. Select `Publish client images`.
3. Click `Run workflow`.
4. Set `version` to `v0.1.0`.
5. Leave `publish_cuda` unchecked for the supported CPU release.
6. Run the workflow.

Tag-based publish:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Tag pushes publish the backend CPU image and frontend image. CUDA publish remains
manual-only through `workflow_dispatch` with `publish_cuda=true`.

## Package Visibility And Access

The source repository may remain private while the GHCR packages are public.
Public packages allow clients to pull the images without `docker login ghcr.io`.

If packages must remain private:

1. Grant the client GitHub account or organization access to the packages.
2. Instruct the client to authenticate before startup:

   ```bash
   docker login ghcr.io
   ```

Do not commit personal access tokens, fine-grained tokens, classic PATs, or client
credentials to this repository or deployment bundle.

## Verify Package Publication

After the workflow succeeds, verify package pages in GitHub:

```text
https://github.com/users/taha328/packages/container/package/building-change-backend
https://github.com/users/taha328/packages/container/package/building-change-frontend
```

Confirm expected tags exist:

```text
cpu-v0.1.0
cpu-latest
v0.1.0
latest
```

Confirm both CPU delivery images include:

```text
linux/amd64
linux/arm64
```

Confirm the selected PostGIS image also includes both platforms:

```bash
docker buildx imagetools inspect imresamu/postgis:16-3.4
```

For public packages, verify manifests and pulls after logging out:

```bash
docker logout ghcr.io || true
docker buildx imagetools inspect ghcr.io/taha328/building-change-backend:cpu-v0.1.0
docker buildx imagetools inspect ghcr.io/taha328/building-change-frontend:v0.1.0
docker pull ghcr.io/taha328/building-change-backend:cpu-v0.1.0
docker pull ghcr.io/taha328/building-change-frontend:v0.1.0
```

## Validate Pull From Deploy Bundle

From the repository root after images are published:

```bash
cd deploy
cp .env.example .env.ghcr-test
docker compose --env-file .env.ghcr-test pull frontend backend-api celery-worker
cd ..
```

Expected application images:

```text
ghcr.io/taha328/building-change-backend:cpu-v0.1.0
ghcr.io/taha328/building-change-frontend:v0.1.0
```

No registry login is required when the packages are public. If the packages are
private, run `docker login ghcr.io` first.

## Clean GHCR Deployment Validation

After images are pullable:

```bash
rm -rf /tmp/building-change-ghcr-test
mkdir -p /tmp/building-change-ghcr-test
cp -R deploy/. /tmp/building-change-ghcr-test/
cp vendor/BANDON-mps/checkpoints/mtgcdnet_iter_40000.pth /tmp/building-change-ghcr-test/models/bandon/
cd /tmp/building-change-ghcr-test
cp .env.example .env
docker compose pull
./scripts/start.sh
./scripts/health.sh
./scripts/validate-runtime.sh
./scripts/smoke-test.sh
./scripts/stop.sh
```

Passing criteria:

- Images pull from GHCR.
- Stack starts from the deployment bundle only.
- Frontend responds.
- Backend, database, Redis, and jobs health checks pass.
- Runtime validation resolves `MODEL_DEVICE=auto` to `cpu`.
- Smoke job completes.
- `/api/files` retrieves at least one artifact.

## Local Emergency Publish

Prefer GitHub Actions. If a local emergency push is needed, authenticate first:

```bash
docker login ghcr.io
```

Then build, tag, and push manually without including the checkpoint in any image.
Do not store tokens in shell scripts or files.
