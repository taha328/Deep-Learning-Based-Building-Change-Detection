# Building Change Detection

Building Change Detection is a local-first platform for detecting, reviewing, and exporting building additions and changes across historical satellite imagery. Users define an area of interest, select Esri Wayback releases, run local deep-learning inference, inspect temporal metrics and map layers, and export geospatial results.

## What The Platform Does

- Discovers high-resolution historical imagery from Esri Wayback.
- Validates AOIs and estimates imagery and inference workloads.
- Downloads, caches, mosaics, and aligns imagery for selected dates.
- Runs local building-change inference and derives cleaned vector products.
- Tracks temporal projects, milestones, growth metrics, and generated artifacts.
- Exports raster and vector results for GIS workflows, including QGIS-compatible outputs.

## Architecture

The React frontend calls a FastAPI API. FastAPI handles validation and synchronous operations, while Redis and Celery support queued processing. PostgreSQL/PostGIS stores project, run, artifact, and spatial metadata when postgres persistence is enabled. Large imagery, inference outputs, exports, and caches remain on disk or in the Docker `runtime_cache` volume.

The processing path is:

```text
AOI + Wayback releases
  -> imagery metadata and tile retrieval
  -> cached mosaics and reference imagery
  -> BANDON inference
  -> raster cleanup and vectorization
  -> temporal metrics, map layers, and exports
```

## Technology Stack

- Frontend: React 18, Vite, TypeScript, Zustand, React Query, MapLibre, Tailwind
- API and workers: FastAPI, Celery, Redis
- Persistence: PostgreSQL 16, PostGIS, SQLAlchemy, GeoAlchemy2, Alembic
- Geospatial processing: GDAL, Rasterio, GeoPandas, Shapely, PyProj, Rio-Tiler
- Inference: PyTorch and the patched repository under `vendor/BANDON-mps`
- Deployment: Docker and Docker Compose

## Deep-Learning Runtime

`bandon_mps` is the only supported selectable inference backend. The patched BANDON runtime supports:

- `MODEL_DEVICE=auto`: CUDA when available, then native macOS MPS when available, otherwise CPU.
- `MODEL_DEVICE=cpu`: always CPU.
- `MODEL_DEVICE=cuda`: requires an available CUDA device and fails clearly otherwise.
- `MODEL_DEVICE=mps`: requires available native macOS MPS and fails clearly otherwise.

`APP_BANDON_DEVICE` remains a backward-compatible fallback, but `MODEL_DEVICE` takes precedence.

The active BANDON config and checkpoint retain internal MTGCDNet filenames:

```text
vendor/BANDON-mps/workdirs_bandon/MTGCDNet/config.py
vendor/BANDON-mps/checkpoints/mtgcdnet_iter_40000.pth
```

Those names do not represent a separate selectable MTGCDNet backend. Checkpoints are external artifacts: they are not committed to Git or baked into application images.

## Repository Structure

```text
backend/             FastAPI API, workers, processing pipeline, migrations, and tests
frontend/            React/Vite application
deploy/              Packaged Docker deployment and operational scripts
docker/              Source-build Dockerfiles
docs/                Focused model, release, and Wayback technical notes
qgis_plugin/         QGIS integration source
scripts/             Development and model-artifact utilities
shared/              API contract snapshot
vendor/BANDON-mps/   Patched BANDON runtime
```

## Prerequisites

Recommended packaged deployment:

- Docker Engine or Docker Desktop with Docker Compose
- `linux/amd64` or `linux/arm64`
- At least 16 GB RAM; 24 GB or more is recommended for CPU inference
- Sufficient disk for images, PostgreSQL data, imagery caches, and exports
- The separately distributed BANDON checkpoint artifact

Source development additionally requires Python 3, Node.js/npm, and system geospatial libraries compatible with the pinned GDAL Python package.

## Recommended Docker Setup

The packaged deployment under `deploy/` runs the full stack: `postgres`, `redis`, `backend-api`, `celery-worker`, and `frontend`. CPU Docker is the supported default. The optional CUDA override is not production-certified and requires separate validation on a compatible NVIDIA host.

When the latest GitHub Release includes `building-change-app.zip`, the full bundle, including the approved model checkpoint, can be installed and started without cloning the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash
```

The installer uses a versioned local directory and does not overwrite an existing installation. If the latest release does not contain `building-change-app.zip`, use the packaged deployment workflow below and install the model artifact separately.

```bash
git clone https://github.com/taha328/building_change_app.git
cd building_change_app/deploy
cp .env.example .env
```

Set a non-default `POSTGRES_PASSWORD` in `deploy/.env` before shared deployment. The default images are public GHCR multi-platform CPU images.

Install the separately delivered model artifact:

```bash
MODEL_ARTIFACT_FILE=/path/to/building-change-model-bandon-mtgcdnet-v0.1.1.zip ./scripts/fetch-model.sh
```

Alternatively, manually place the checkpoint at:

```text
deploy/models/bandon/mtgcdnet_iter_40000.pth
```

Start and validate the stack:

```bash
./scripts/start.sh
./scripts/health.sh
./scripts/validate-runtime.sh
./scripts/smoke-test.sh
```

Open:

- Application: `http://127.0.0.1:8080`
- API: `http://127.0.0.1:8000`
- API documentation: `http://127.0.0.1:8000/docs`

Stop without deleting persisted data:

```bash
./scripts/stop.sh
```

To remove the stack and its persisted Docker volumes intentionally:

```bash
docker compose down -v
```

Windows PowerShell equivalents are available under `deploy/scripts/windows/`.

## Environment Configuration

The principal deployment variables are defined in `deploy/.env.example`:

| Variable | Purpose |
| --- | --- |
| `BACKEND_IMAGE`, `FRONTEND_IMAGE`, `POSTGIS_IMAGE` | Container image selection |
| `FRONTEND_PORT`, `BACKEND_PORT` | Local bound ports |
| `MODEL_DEVICE` | `auto`, `cpu`, `cuda`, or `mps` runtime selection |
| `APP_INFERENCE_BACKEND` | Must remain `bandon_mps` |
| `APP_CHANGE_THRESHOLD`, `APP_SEMANTIC_THRESHOLD` | Inference thresholds |
| `APP_BANDON_CHECKPOINT_PATH` | Checkpoint path inside the backend container |
| `APP_RUNTIME_CACHE_DIR` | Generated artifact and cache root |
| `PERSISTENCE_BACKEND`, `DATABASE_URL` | Persistence mode and PostgreSQL connection |
| `REDIS_URL` | Celery broker/result backend connection |
| `CORS_ALLOWED_ORIGINS` | Browser origins allowed by FastAPI |

Esri Wayback access uses public service endpoints and requires no API key. A Mapbox public token is needed only for optional Mapbox-backed frontend/current-imagery features; set `VITE_MAPBOX_API_KEY` for frontend development and `MAPBOX_ACCESS_TOKEN` for backend current-imagery retrieval.

## Source Development

Start PostgreSQL and Redis, initialize PostGIS, then run the local stack:

```bash
docker compose up -d postgres redis
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
python scripts/setup_postgis_db.py --migrate --verify
cd ..
./scripts/dev_start_all.sh
```

The application runs at `http://127.0.0.1:5173`. Stop it with `CTRL+C` or:

```bash
./scripts/dev_stop_all.sh
```

Manual backend and worker startup:

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export PERSISTENCE_BACKEND=postgres
export DATABASE_URL=postgresql+psycopg://building_change:building_change@localhost:5432/building_change
export REDIS_URL=redis://localhost:6379/0
python scripts/setup_postgis_db.py --migrate --verify
python scripts/start_backend.py
```

In another terminal:

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
python scripts/verify_worker_env.py
python scripts/start_celery_worker.py
```

In another terminal:

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev:local
```

On macOS, the worker launcher uses Celery's `solo` pool because the geospatial and ML stack is not safe under the default prefork workflow.

## Source Docker Builds

The root Compose file builds the CPU backend and worker while exposing PostgreSQL and Redis. It does not include the packaged frontend service.

```bash
docker compose build backend-api
docker compose up -d postgres redis
docker compose run --rm backend-api /app/backend/.venv/bin/python /app/backend/scripts/setup_postgis_db.py --migrate --verify
docker compose up -d backend-api celery-worker
curl http://127.0.0.1:8000/api/health
```

For CUDA development on a Linux NVIDIA host:

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml build backend-api
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm backend-api \
  /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json
```

CUDA requires a compatible NVIDIA driver, NVIDIA Container Toolkit, Docker GPU support, and target-host validation. Mac Docker is not a CUDA target; native macOS MPS is a separate local runtime path.

## Tests

```bash
cd backend
source .venv/bin/activate
PYTHONNOUSERSITE=1 pytest
```

```bash
cd frontend
npm install
npm test
npm run build
```

Validate a built or packaged BANDON runtime with:

```bash
cd backend
MODEL_DEVICE=auto ./.venv/bin/python scripts/validate_bandon_runtime.py --json
```

## Cache And Storage Policy

- PostgreSQL/PostGIS stores durable project, run, artifact, and selected spatial metadata.
- Redis is the Celery broker/result backend; it is not the durable artifact store.
- Heavy imagery, rasters, previews, vector exports, and request workspaces live under `APP_RUNTIME_CACHE_DIR`.
- Wayback tiles use a SQLite WAL cache by default, with the legacy file cache as fallback.
- Reference imagery is shared through canonical cached COGs where possible.
- Completed request workspaces are compacted according to post-completion cleanup settings while project-owned artifacts and provenance are preserved.
- Docker volumes `postgres_data`, `redis_data`, and `runtime_cache` persist across ordinary stops.

Do not run `docker compose down -v` unless deleting persisted data is intentional.

## Operational Notes

- CPU Docker is the supported packaged runtime.
- Native MPS is supported for local macOS development.
- CUDA is optional and is not production-certified.
- First runs may be slow while Wayback metadata, tiles, and imagery caches populate.
- Wayback inference prefers zoom 18 and may fall back to a safe lower zoom when coverage preflight requires it.
- Raw `uvicorn --reload` from `backend/` may watch `.venv`; use `backend/scripts/start_backend.py`.
- GDAL system libraries and Python bindings must remain version-compatible.

## Troubleshooting

- **Missing checkpoint:** verify `deploy/models/bandon/mtgcdnet_iter_40000.pth` for packaged deployment or the configured `APP_BANDON_CHECKPOINT_PATH`.
- **Backend unhealthy:** run `deploy/scripts/health.sh`, then inspect `deploy/scripts/logs.sh backend-api`.
- **Job remains queued:** inspect `celery-worker` logs and Redis health.
- **Database unhealthy:** verify Docker resources, credentials, and `postgres` logs.
- **Frontend cannot reach FastAPI:** verify the backend URL and `CORS_ALLOWED_ORIGINS`.
- **Port already in use:** change `FRONTEND_PORT` or `BACKEND_PORT` in `deploy/.env`, or stop stale local processes.
- **Slow CPU inference:** reduce AOI size for validation and allocate more Docker CPU/RAM.
- **GDAL installation failure:** compare `gdal-config --version` with the pinned `gdal` version in `backend/requirements.txt`.

Focused operational details remain in [`docs/model-artifact.md`](docs/model-artifact.md), [`docs/release-ghcr.md`](docs/release-ghcr.md), [`docs/wayback_tile_ingestion.md`](docs/wayback_tile_ingestion.md), and [`backend/docs/cache_strategy.md`](backend/docs/cache_strategy.md).
