# Building Change Detection Backend

This backend exposes the production FastAPI API used by the React frontend.

The backend keeps the existing processing pipeline intact:

- Wayback release discovery
- AOI validation
- imagery download and mosaics
- co-registration
- building extraction and change detection
- temporal project validation and execution
- artifact generation and file serving

## Run Locally

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
export PERSISTENCE_BACKEND=postgres
export REDIS_URL="redis://localhost:6379/0"
python scripts/start_backend.py
```

Open the API docs at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

Use `scripts/start_backend.py` for reload mode. Raw `uvicorn --reload` from the backend directory can watch `.venv` and continuously reload when packages change.

## Optional PostgreSQL/PostGIS Persistence

Filesystem persistence remains the default and keeps generated rasters, previews, bundles, and exports on disk.

To mirror durable project metadata and vector state into PostgreSQL/PostGIS, use the automatic setup workflow below.

## Automatic PostgreSQL/PostGIS Setup

```bash
cd backend
python scripts/setup_postgis_db.py --migrate --verify
```

This command:
- creates the target role when missing,
- creates the target database when missing,
- enables the PostGIS extension,
- runs Alembic migrations,
- verifies expected tables.

The script is safe to rerun.

### Docker PostgreSQL/PostGIS Path

```bash
docker compose up -d postgres redis
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export PERSISTENCE_BACKEND=postgres
export DATABASE_URL=postgresql+psycopg://building_change:building_change@localhost:5432/building_change
export DATABASE_ECHO=false
export REDIS_URL="redis://localhost:6379/0"
python scripts/setup_postgis_db.py --migrate --verify
python scripts/start_backend.py
curl http://127.0.0.1:8000/api/health/db
```

### Local PostgreSQL Path

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export DATABASE_URL=postgresql+psycopg://building_change:building_change@localhost:5432/building_change
export PERSISTENCE_BACKEND=postgres
export REDIS_URL="redis://localhost:6379/0"
python scripts/setup_postgis_db.py --migrate --verify
python scripts/start_backend.py
curl http://127.0.0.1:8000/api/health/db
```

### Windows PowerShell Path

```powershell
cd backend
$env:DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
$env:PERSISTENCE_BACKEND="postgres"
python scripts/setup_postgis_db.py --migrate --verify
python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/api/health/db
```

Verify database health independently at:

```bash
curl http://127.0.0.1:8000/api/health/db
```

Large generated files remain in the filesystem artifact store. PostgreSQL stores project metadata, AOI and selected vector geometries, milestones, metrics, run metadata, and artifact metadata.

Use `make postgres-setup` from the repository root for the same automated setup.

Async job execution is available through Redis + Celery. Start Redis with `docker compose up -d redis`, keep `JOBS_ENABLED=true`, and use `/api/jobs/*` for queued detection and temporal project runs.

### Local macOS worker

Use the solo pool locally to avoid the macOS worker crashes that can happen with prefork:

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
export PERSISTENCE_BACKEND=postgres
export REDIS_URL="redis://localhost:6379/0"
python scripts/verify_worker_env.py
python scripts/start_celery_worker.py
```

macOS local Celery must use the `solo` pool for this geospatial/ML stack.

The launcher defaults to:
- `CELERY_WORKER_POOL=solo`
- `CELERY_WORKER_CONCURRENCY=1`
- `CELERY_TASK_ACKS_LATE=false`
- `CELERY_TASK_REJECT_ON_WORKER_LOST=false`
- `CELERY_WORKER_PREFETCH_MULTIPLIER=1`

For Linux or production workers, override the pool explicitly if you want prefork behavior:

```bash
cd backend
CELERY_WORKER_POOL=prefork CELERY_WORKER_CONCURRENCY=4 ./.venv/bin/python scripts/start_celery_worker.py
```

Verify the interpreter and site-packages isolation with:

```bash
cd backend
PYTHONNOUSERSITE=1 ./.venv/bin/python scripts/verify_worker_env.py
```

## Backend Modes

The FastAPI API is the only browser-facing API.

Processing can still use different execution backends behind FastAPI:

- `bandon_mps` for the local BANDON pipeline on Apple Silicon
- `sam3` for SAM3-based execution
- optional remote SAM3 execution through a backend-only `gradio_client` adapter

## Environment Overrides

Common environment variables:

- `APP_RUNTIME_CACHE_DIR`
- `APP_FAST_PREVIEW_MAX_AREA_M2`
- `APP_FAST_PREVIEW_MAX_SCENE_TILES`
- `APP_FULL_RUN_MAX_AREA_M2`
- `APP_FULL_RUN_MAX_SCENE_TILES`
- `APP_DOWNLOAD_WORKERS`
- `APP_BUFFER_DISTANCES_M`
- `APP_MODEL_BACKEND_DEFAULT`
- `APP_REMOTE_SEGMENTATION_SPACE`
- `APP_REMOTE_SEGMENTATION_API_NAME`
- `APP_REMOTE_SEGMENTATION_PROMPT`
- `APP_REMOTE_SEGMENTATION_TIMEOUT_SEC`
- `APP_REMOTE_SEGMENTATION_RETRIES`
- `APP_BANDON_REPO_DIR`
- `APP_BANDON_ENV_PREFIX`
- `APP_BANDON_CONFIG_PATH`
- `REDIS_URL`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`
- `CELERY_TASK_DEFAULT_QUEUE`
- `CELERY_WORKER_POOL`
- `CELERY_WORKER_CONCURRENCY`
- `CELERY_TASK_ACKS_LATE`
- `CELERY_TASK_REJECT_ON_WORKER_LOST`
- `CELERY_WORKER_PREFETCH_MULTIPLIER`
- `CELERY_JOB_STALE_AFTER_MINUTES`
- `JOBS_ENABLED`
- `APP_BANDON_CHECKPOINT_PATH`
- `APP_BANDON_DEVICE`
- `APP_BANDON_ALLOW_MPS_FALLBACK`
- `CORS_ALLOWED_ORIGINS`
- `CORS_ALLOW_ORIGIN_REGEX`

## macOS / GDAL Note

Raster alignment uses reprojection-only alignment through Rasterio/GDAL, so the installed GDAL library and Python bindings still need to match.

If dependency installation fails with a GDAL mismatch, verify:

```bash
gdal-config --version
```

and make sure it matches the pinned `gdal==...` version in `requirements.txt`.
