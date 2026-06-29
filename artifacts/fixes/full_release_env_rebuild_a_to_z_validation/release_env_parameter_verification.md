# Release Env Parameter Verification

Date: 2026-06-29

Release bundle verified from the public raw-installer output at:

`/Users/tahaelouali/.local/share/building-change-app/releases/20260629T130818Z.22gBGj/building-change-app`

## Installed `.env`

Required release image tags:

- `BACKEND_IMAGE=ghcr.io/taha328/building-change-backend:cpu-v0.1.5`
- `FRONTEND_IMAGE=ghcr.io/taha328/building-change-frontend:v0.1.5`

Required public Mapbox runtime values:

- `MAPBOX_API_KEY=pk.eyJ1IjoidGFoYWVsIiwiYSI6ImNtbnl6dHdqcjA3Z3EycXNmZHQyM3FkZWQifQ.IDf_zeGoMaPHcrsLOD5q7A`
- `MAPBOX_ACCESS_TOKEN=pk.eyJ1IjoidGFoYWVsIiwiYSI6ImNtbnl6dHdqcjA3Z3EycXNmZHQyM3FkZWQifQ.IDf_zeGoMaPHcrsLOD5q7A`

Required safe app defaults present:

- `APP_CHANGE_THRESHOLD=0.50`
- `APP_SEMANTIC_THRESHOLD=0.50`
- `APP_MAPBOX_MAX_TILES_PER_REQUEST=1024`
- `MAPBOX_CURRENT_IMAGERY_MAX_TILES=1024`
- `APP_WAYBACK_DEFAULT_ZOOM=18`
- `APP_TILE_ZOOM=18`
- `APP_WAYBACK_HTTP_CONNECT_TIMEOUT_SECONDS=60`
- `APP_WAYBACK_HTTP_READ_TIMEOUT_SECONDS=120`
- `APP_WAYBACK_HTTP_MAX_RETRIES=8`
- `APP_WAYBACK_HTTP_BACKOFF_BASE_SECONDS=1.0`
- `APP_WAYBACK_TILE_MAX_CONCURRENCY=12`
- `APP_WAYBACK_MAX_MISSING_TILE_RATIO=0.05`
- `APP_POST_COMPLETION_REQUEST_CLEANUP_ENABLED=true`
- `APP_POST_COMPLETION_REQUEST_CLEANUP_MODE=compact_heavy`
- `APP_POST_COMPLETION_REQUEST_CLEANUP_GRACE_SECONDS=300`
- `APP_POST_COMPLETION_REQUEST_CLEANUP_KEEP_PROVENANCE=true`
- `APP_POST_COMPLETION_REQUEST_CLEANUP_DELETE_EXPORT_BUNDLE=true`

`APP_S2LOOKING_CHECKPOINT_PATH` was not packaged. The release keeps `APP_INFERENCE_BACKEND=bandon_mps` because the Docker CPU runtime resolves `MODEL_DEVICE=auto` to CPU for the BANDON runtime.

## Compose Resolution

`docker compose --env-file .env config` resolved:

- backend image: `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`
- frontend image: `ghcr.io/taha328/building-change-frontend:v0.1.5`
- backend env: `APP_MAPBOX_MAX_TILES_PER_REQUEST: "1024"`
- backend env: `APP_WAYBACK_HTTP_READ_TIMEOUT_SECONDS: "120"`
- backend env: `APP_POST_COMPLETION_REQUEST_CLEANUP_MODE: compact_heavy`
- backend env: `MAPBOX_ACCESS_TOKEN: pk...`
- frontend env: `MAPBOX_API_KEY: pk...`

## Runtime Config

`http://127.0.0.1:8080/runtime-config.js` from the installed Docker frontend returned:

```js
window.BUILDING_CHANGE_RUNTIME_CONFIG = {
  VITE_FASTAPI_BACKEND_URL: window.location.origin,
  MAPBOX_API_KEY: "pk.eyJ1IjoidGFoYWVsIiwiYSI6ImNtbnl6dHdqcjA3Z3EycXNmZHQyM3FkZWQifQ.IDf_zeGoMaPHcrsLOD5q7A",
};
```
