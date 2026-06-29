# Raw Installer Validation

Date: 2026-06-29

Command run exactly:

```sh
curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash
```

Installed bundle:

`/Users/tahaelouali/.local/share/building-change-app/releases/20260629T130818Z.22gBGj/building-change-app`

Installer result:

- Downloaded current release package.
- Pulled `ghcr.io/taha328/building-change-frontend:v0.1.5`.
- Pulled `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`.
- Created Docker network and containers.
- Started Redis and PostGIS.
- Ran PostgreSQL/PostGIS setup and Alembic migrations.
- Started backend API, Celery worker, and frontend.

Running containers after install:

- `building-change-frontend-1`: `ghcr.io/taha328/building-change-frontend:v0.1.5`, healthy, `127.0.0.1:8080->80`
- `building-change-backend-api-1`: `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`, healthy, `127.0.0.1:8000->8000`
- `building-change-celery-worker-1`: `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`
- `building-change-redis-1`: `redis:7-alpine`, healthy
- `building-change-postgres-1`: `imresamu/postgis:16-3.4`, healthy

Bundled script checks:

```text
./scripts/health.sh
Health checks passed.

./scripts/validate-runtime.sh
device_requested=auto
device_resolved=cpu
torch_version=2.12.1+cpu
mmcv_version=1.7.0
Runtime validation passed.

./scripts/smoke-test.sh
job_id=job-aad23e11a3e7467394c1865309a9577e
status=completed progress=100 stage=completed
request_hash=25d8e23d09315e8fd2cdc44b
device_resolved=cpu
artifact_count=14
retrieved_png=/data/runtime_cache/requests/25d8e23d09315e8fd2cdc44b/t1_preview.png
Smoke test passed.
```
