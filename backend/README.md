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
./.venv/bin/python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
```

Open the API docs at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Optional PostgreSQL/PostGIS Persistence

Filesystem persistence remains the default and keeps generated rasters, previews, bundles, and exports on disk.

To mirror durable project metadata and vector state into PostgreSQL/PostGIS:

```bash
docker compose up -d postgres
```

```bash
cd backend
export PERSISTENCE_BACKEND=postgres
export DATABASE_URL=postgresql+psycopg://building_change:building_change@localhost:5432/building_change
export DATABASE_ECHO=false
alembic upgrade head
./.venv/bin/python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
```

Verify database health:

```bash
curl http://127.0.0.1:8000/api/health/db
```

Large generated files remain in the filesystem artifact store. PostgreSQL stores project metadata, AOI and selected vector geometries, milestones, metrics, run metadata, and artifact metadata.

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
- `APP_BANDON_CHECKPOINT_PATH`
- `APP_BANDON_DEVICE`
- `APP_BANDON_ALLOW_MPS_FALLBACK`

## macOS / AROSICS Note

The local co-registration path uses [AROSICS](https://danschef.git-pages.gfz-potsdam.de/arosics/doc/) and needs GDAL Python bindings that match the installed GDAL library.

If dependency installation fails with a GDAL mismatch, verify:

```bash
gdal-config --version
```

and make sure it matches the pinned `gdal==...` version in `requirements.txt`.
