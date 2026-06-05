# Building Change Detection

This monorepo contains a React frontend and a local-first FastAPI backend:

- [`frontend/`](./frontend): React + Vite application
- [`backend/`](./backend): FastAPI API plus local processing pipeline

The frontend talks only to FastAPI. Inference is local-only through `bandon_mps` or `mtgcdnet_s2looking_mps`.

## Repository Layout

```text
frontend/  # React + Vite UI
backend/   # FastAPI API and processing pipeline
shared/    # API contract snapshot
notebook_.ipynb  # original scientific workflow
```

## Local Run

One-command dev stack:

```bash
./scripts/dev_start_all.sh
```

Open:

```text
http://127.0.0.1:5173/
```

Keep the `dev_start_all.sh` terminal open. Pressing `CTRL+C` stops the frontend, backend, and Celery worker; if the browser says connection refused, start the stack again. To stop stale dev processes and free ports `5173`, `5174`, and `8000` manually:

```bash
./scripts/dev_stop_all.sh
```

Backend:

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
export PERSISTENCE_BACKEND=postgres
export REDIS_URL="redis://localhost:6379/0"
python scripts/start_backend.py
```

Local Celery worker:

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

Use `scripts/start_backend.py` for local reload mode. Raw `uvicorn --reload` from the backend directory can watch `.venv` and reload forever when site-packages change. On macOS, the local Celery worker uses the `solo` pool for this geospatial/ML stack.

Optional PostgreSQL/PostGIS persistence:

```bash
docker compose up -d postgres redis
cd backend
python scripts/setup_postgis_db.py --migrate --verify
```

## CPU Docker Run

The backend CPU image mounts the BANDON checkpoint instead of baking it into the image. Keep the checkpoint at:

```text
vendor/BANDON-mps/checkpoints/mtgcdnet_iter_40000.pth
```

Build and validate the CPU runtime:

```bash
docker compose build backend-api
docker compose run --rm -e MODEL_DEVICE=auto backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py
docker compose run --rm -e MODEL_DEVICE=cpu backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py
```

Run database migrations explicitly, then start the API and worker:

```bash
docker compose up -d postgres redis
docker compose run --rm backend-api /app/backend/.venv/bin/python /app/backend/scripts/setup_postgis_db.py --migrate --verify
docker compose up -d backend-api celery-worker
curl http://127.0.0.1:8000/api/health
```

## CUDA Docker Run

The default Compose stack remains CPU-only:

```bash
docker compose up -d postgres redis backend-api celery-worker
```

CUDA Docker is an override for Linux NVIDIA hosts. It requires an NVIDIA GPU,
a compatible host driver, NVIDIA Container Toolkit, and Docker Compose GPU
support. Mac Docker is not a CUDA target; native macOS MPS remains the separate
local runtime path.

Build and start the CUDA backend image:

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml build backend-api
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d postgres redis backend-api celery-worker
```

Validate GPU visibility and PyTorch CUDA inside the container:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:<verified-tag> nvidia-smi
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm backend-api nvidia-smi
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm backend-api /app/backend/.venv/bin/python - <<'PY'
import torch
print("torch", torch.__version__)
print("torch_cuda_version", torch.version.cuda)
print("cuda_available", torch.cuda.is_available())
print("device_count", torch.cuda.device_count())
print("device_name", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
PY
```

Validate BANDON device modes:

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm -e MODEL_DEVICE=auto backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm -e MODEL_DEVICE=cuda backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm -e MODEL_DEVICE=cpu backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json
docker compose -f docker-compose.yml -f docker-compose.cuda.yml run --rm -e MODEL_DEVICE=mps backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json
```

Expected CUDA-host behavior:

```text
MODEL_DEVICE=auto -> cuda when the GPU is visible, otherwise cpu
MODEL_DEVICE=cuda -> cuda when visible, clear failure when unavailable
MODEL_DEVICE=cpu  -> cpu even on a GPU host
MODEL_DEVICE=mps  -> clear unavailable failure inside Linux CUDA Docker
```

## Automatic PostgreSQL/PostGIS Setup

For local PostgreSQL/PostGIS:

```bash
cd backend
python scripts/setup_postgis_db.py --migrate --verify
```

This command:
- creates the `building_change` role if missing,
- creates the `building_change` database if missing,
- enables the PostGIS extension,
- runs Alembic migrations,
- verifies expected tables.

Environment override:

```bash
cd backend
DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change" python scripts/setup_postgis_db.py --migrate --verify
```

Docker path:

```bash
docker compose up -d postgres redis
cd backend
python scripts/setup_postgis_db.py --migrate --verify
```

Backend run path:

```bash
cd backend
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export PERSISTENCE_BACKEND=postgres
export DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
export REDIS_URL="redis://localhost:6379/0"
python scripts/start_backend.py
curl http://127.0.0.1:8000/api/health/db
```

Windows PowerShell:

```powershell
cd backend
$env:PERSISTENCE_BACKEND="postgres"
$env:DATABASE_URL="postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
python scripts/setup_postgis_db.py --migrate --verify
python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/api/health/db
```

`setup_postgis_db.py` creates the database and enables PostGIS. Alembic creates application tables.
Rows are created only after saving/running temporal projects in postgres mode.
Large raster/artifact files remain on disk by design.

One-command setup is also available from repo root:

```bash
make postgres-setup
```

Frontend:

```bash
cd frontend
VITE_FASTAPI_BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

Async jobs use Redis + Celery when enabled. If Redis is unavailable, the frontend still falls back to the synchronous FastAPI run endpoints.

## Pipeline Notes

The geospatial and model pipeline remains unchanged:

- live Wayback release discovery
- AOI validation and tile-budget guards
- local MTGCDNet change detection through `bandon_mps` or `mtgcdnet_s2looking_mps`
- change score derivation, cleanup, vectorization, blocks, buffers, and exports

## API Contract Snapshot

The request/response contract snapshot is stored at [`shared/api-contract.json`](./shared/api-contract.json).
