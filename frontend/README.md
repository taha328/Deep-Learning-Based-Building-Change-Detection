# Building Change Detection Frontend

This is the React + Vite frontend for the Building Change application.

The frontend talks only to the FastAPI backend.

## Runtime Variables

Supported frontend variables:

- `VITE_FRONTEND_MODE=local|remote`
- `VITE_FASTAPI_BACKEND_URL=http://127.0.0.1:8000`
- `VITE_LOCAL_BACKEND_URL=http://127.0.0.1:8000`
- `VITE_BACKEND_URL=...`
- `VITE_SHOW_BACKEND_SELECTOR=true|false`
- `VITE_ENABLE_REQUEST_BACKEND_SELECTION=true|false`
- `VITE_MAPBOX_API_KEY=...`

## Local Development

1. Start the backend locally:

```bash
cd /Users/tahaelouali/Desktop/Building_change_app/backend
APP_INFERENCE_BACKEND=bandon_mps APP_BANDON_DEVICE=mps ./.venv/bin/python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
```

2. Start the frontend locally:

```bash
cd /Users/tahaelouali/Desktop/Building_change_app/frontend
cp .env.example .env.local
npm install
npm run dev:local
```

The frontend will default to:

- frontend URL: `http://127.0.0.1:5173`
- backend URL: `http://127.0.0.1:8000`
- preferred model backend: `bandon_mps`

## Remote Compatibility Mode

To point the frontend at another FastAPI backend:

```bash
cd /Users/tahaelouali/Desktop/Building_change_app/frontend
npm run dev:remote
```

or set these in Cloudflare Pages:

- `VITE_FRONTEND_MODE=remote`
- `VITE_BACKEND_URL=https://your-fastapi-host.example.com`
- `VITE_MAPBOX_API_KEY=...`

## Cloudflare Pages

Recommended project name:

- `taha321-building-change-frontend`

Build settings:

- Framework preset: `Vite`
- Build command: `npm run build`
- Build output directory: `dist`
- Root directory: `frontend`

Wrangler deploy:

```bash
cd /Users/tahaelouali/Desktop/Building_change_app/frontend
npm install
npm run build
npm run pages:deploy -- --project-name taha321-building-change-frontend
```

## Notes

- The frontend does not duplicate backend logic. It calls the FastAPI API only.
- The local workflow is intended to match the practical parameter set exposed by the QGIS plugin.
- Backend inference is local-only through `bandon_mps` or `mtgcdnet_s2looking_mps`.
