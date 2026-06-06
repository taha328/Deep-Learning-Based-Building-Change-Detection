# Building Change Detection Client Deployment

This folder is the client deployment bundle for a single-machine Docker deployment.
It is not the development source tree.

## What Is Included

- `docker-compose.yml`: supported CPU Docker stack.
- `docker-compose.cuda.yml`: optional CUDA override, pending target-machine validation.
- `.env.example`: client environment template.
- `models/bandon/`: place the external BANDON checkpoint here.
- `scripts/`: macOS/Linux helper scripts.
- `scripts/windows/`: Windows PowerShell helper scripts.

## System Requirements

- Docker Desktop or Docker Engine with Docker Compose support.
- A Docker-supported `linux/amd64` or `linux/arm64` host.
- Internet access for first startup unless images are preloaded.
- 16 GB RAM minimum; 24 GB or more is recommended for smoother CPU inference.
- Enough disk space for Docker images, database data, runtime cache, and exported artifacts.

The client does not need Python, Node, Conda, MPS setup, or the full source repository.

## Container Images

The deployment pulls application images from GitHub Container Registry:

```text
ghcr.io/taha328/building-change-backend:cpu-v0.1.0
ghcr.io/taha328/building-change-frontend:v0.1.0
```

The CPU backend and frontend images are multi-platform images for `linux/amd64`
and `linux/arm64`. Docker automatically pulls the correct variant for the host
architecture. They run on Docker-supported hosts with sufficient RAM and disk.

The client stack uses `imresamu/postgis:16-3.4` for native PostGIS support on
both `linux/amd64` and `linux/arm64`. This image preserves PostgreSQL 16 and
PostGIS 3.4 compatibility and identifies its source as the upstream
`postgis/docker-postgis` project. Override `POSTGIS_IMAGE` only after validating
the replacement image on the target architecture.

The source repository may remain private while these GHCR packages are public.
No registry login is required for the public client images. If a future release
uses private packages, sign in before startup:

```bash
docker login ghcr.io
```

Use credentials or a token provided through your organization. Do not paste tokens
into `.env` or any deployment file.

## Windows Notes

Use Docker Desktop with the WSL2 backend. Allocate enough CPU and memory in Docker
Desktop settings before running smoke tests. Run PowerShell from this deployment
folder. CUDA on Windows requires a compatible NVIDIA GPU, driver, WSL2 GPU support,
Docker GPU support, and separate validation.

## Checkpoint Placement

The model checkpoint is external and is not baked into any Docker image.

Place:

```text
models/bandon/mtgcdnet_iter_40000.pth
```

The stack mounts this folder read-only at:

```text
/models/bandon
```

## First Startup

macOS/Linux:

```bash
cp .env.example .env
./scripts/start.sh
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
.\scripts\windows\start.ps1
```

If `.env` does not exist, the start scripts create it from `.env.example`.

## Open Application

Open:

```text
http://127.0.0.1:8080
```

If you change `FRONTEND_PORT` in `.env`, use that port instead.

## Health Check

macOS/Linux:

```bash
./scripts/health.sh
```

Windows:

```powershell
.\scripts\windows\health.ps1
```

The health script checks the frontend, backend, database, Redis, and backend registry endpoint.

## Runtime Validation

macOS/Linux:

```bash
./scripts/validate-runtime.sh
```

Windows:

```powershell
.\scripts\windows\validate-runtime.ps1
```

In the supported CPU deployment, `MODEL_DEVICE=auto` should resolve to `cpu`.

## Smoke Test

macOS/Linux:

```bash
./scripts/smoke-test.sh
```

Windows:

```powershell
.\scripts\windows\smoke-test.ps1
```

The smoke test validates a small known AOI, submits an async detection job, waits for completion,
checks that BANDON resolved to CPU, verifies artifacts, and retrieves one PNG through `/api/files`.

## Stop And Restart

Stop without deleting data:

```bash
./scripts/stop.sh
```

Restart:

```bash
./scripts/restart.sh
```

Windows:

```powershell
.\scripts\windows\stop.ps1
.\scripts\windows\restart.ps1
```

## Logs And Support Bundle

All services:

```bash
./scripts/logs.sh
```

Single service:

```bash
./scripts/logs.sh backend-api
./scripts/logs.sh celery-worker
```

Windows:

```powershell
.\scripts\windows\logs.ps1
.\scripts\windows\logs.ps1 backend-api
```

For support, send the output of:

```bash
./scripts/health.sh
./scripts/validate-runtime.sh
./scripts/logs.sh backend-api
./scripts/logs.sh celery-worker
```

## Where Outputs Are Stored

Runtime outputs are stored in the Docker volume named `runtime_cache`. Database
state is stored in `postgres_data`. Redis state is stored in `redis_data`.

Do not run `docker compose down -v` unless you intentionally want to delete persisted data.

## Update Procedure

1. Stop the stack.
2. Replace this deployment bundle with the new release files, keeping `.env` and `models/bandon/`.
3. Run `./scripts/start.sh` or `.\scripts\windows\start.ps1`.
4. Run health, runtime validation, and smoke test scripts.

## CUDA Status

CPU Docker is the supported default.

CUDA Docker is optional and requires:

- NVIDIA GPU.
- Compatible host driver.
- Docker GPU support.
- A CUDA backend image.
- Target-machine validation.

The optional CUDA image is configured as:

```text
ghcr.io/taha328/building-change-backend:cuda-v0.1.0
```

Do not rely on CUDA until validation passes on the target machine. To try the CUDA
override after validation preparation:

```bash
docker compose --env-file .env -f docker-compose.yml -f docker-compose.cuda.yml up -d
```

## Troubleshooting

- Missing checkpoint: verify `models/bandon/mtgcdnet_iter_40000.pth` exists.
- Frontend does not open: run `./scripts/health.sh` and check `frontend` logs.
- Backend unhealthy: check `backend-api` logs.
- Database unhealthy: check Docker Desktop resources and `postgres` logs.
- Redis unhealthy: check `redis` logs.
- Job remains queued: check `celery-worker` logs.
- CPU inference is slow: this is expected on some laptops; allocate more Docker CPU/RAM.
- Port already in use: change `FRONTEND_PORT` or `BACKEND_PORT` in `.env`.

## Known Limitations

- CPU Docker is the only supported default client delivery mode.
- CPU Docker supports `linux/amd64` and `linux/arm64` hosts with sufficient resources.
- The default client PostGIS image supports native `linux/amd64` and `linux/arm64`.
- CUDA requires separate validation on the target NVIDIA host.
- The checkpoint distribution/license must be handled outside the Docker image.
- First smoke run may take longer while imagery and metadata caches are populated.
