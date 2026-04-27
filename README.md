# Building Change Detection

This monorepo contains a React frontend and a local-first FastAPI backend:

- [`frontend/`](./frontend): React + Vite application
- [`backend/`](./backend): FastAPI API plus local processing pipeline

The frontend talks only to FastAPI. Optional remote SAM3 execution is still supported internally by the backend through `gradio_client`, but that adapter is not exposed to the browser.

## Repository Layout

```text
frontend/  # React + Vite UI
backend/   # FastAPI API and processing pipeline
shared/    # API contract snapshot
notebook_.ipynb  # original scientific workflow
```

## Local Run

Backend:

```bash
cd backend
./.venv/bin/python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
```

Optional PostgreSQL/PostGIS persistence:

```bash
docker compose up -d postgres
cd backend
PERSISTENCE_BACKEND=postgres DATABASE_URL=postgresql+psycopg://building_change:building_change@localhost:5432/building_change alembic upgrade head
```

Frontend:

```bash
cd frontend
VITE_FASTAPI_BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

## Pipeline Notes

The geospatial and model pipeline remains unchanged:

- live Wayback release discovery
- AOI validation and tile-budget guards
- tiled per-date building extraction
- local BANDON MTGCDNet or backend-managed remote SAM3 execution
- change score derivation, cleanup, vectorization, blocks, buffers, and exports

## API Contract Snapshot

The request/response contract snapshot is stored at [`shared/api-contract.json`](./shared/api-contract.json).
