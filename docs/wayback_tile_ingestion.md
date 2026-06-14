# Wayback Tile Ingestion

The backend prefers z18 Wayback imagery for inference and falls back to the closest lower zoom down to `APP_TILE_MIN_ZOOM` when tile availability preflight shows no safe AOI coverage at the preferred zoom. Imagery and mosaic caches are shared across model backends; inference/run caches remain backend-specific elsewhere in the pipeline.

## Runtime knobs

- `APP_WAYBACK_PREFERRED_INFERENCE_ZOOM=18`
- `APP_TILE_MIN_ZOOM=17`
- `APP_WAYBACK_TILE_MIN_CONCURRENCY=4`
- `APP_WAYBACK_TILE_MAX_CONCURRENCY=12`
- `APP_WAYBACK_TILE_CONNECT_TIMEOUT=20`
- `APP_WAYBACK_TILE_READ_TIMEOUT=60`
- `APP_WAYBACK_TILE_MAX_RETRIES=4`
- `APP_WAYBACK_TILE_BACKOFF_BASE=1.0`
- `APP_WAYBACK_TILE_PROGRESS_EVERY_TILES=50`
- `APP_WAYBACK_TILE_PROGRESS_EVERY_SECONDS=5`
- `APP_WAYBACK_TILE_CACHE_BACKEND=sqlite`
- `APP_WAYBACK_TILE_SQLITE_CACHE_DIR=runtime_cache/wayback_tile_cache`

Concurrency guidance:

- 4: conservative shared network
- 8: moderate local development
- 12: default production local-dev setting
- greater than 12: only when using a shared tile cache/proxy and monitoring `/metrics`

## Tile cache strategy

The default tile cache backend is SQLite with WAL enabled. The schema stores tiles by:

- release id
- layer id / tile matrix set
- z/x/y
- content blob
- byte size
- timestamps

The legacy file cache under `runtime_cache/wayback_tiles` remains as a fallback. On SQLite miss, the downloader checks the file cache and promotes hits into SQLite.

## Progress and metrics

Celery job progress includes tile download details:

- preferred zoom and effective zoom
- fallback flag/reason
- processed and total tiles
- cache hits, downloaded, missing, failed
- retries, throttles, timeouts
- tile rate and ETA

The backend exposes a minimal Prometheus-compatible `/metrics` endpoint with Wayback tile counters and download duration summaries.

## MapProxy integration path

For larger deployments, place MapProxy or another WMTS-aware cache in front of the ESRI Wayback WMTS service and set:

```env
APP_WAYBACK_TILE_CACHE_SERVICE_ENABLED=true
APP_WAYBACK_TILE_CACHE_SERVICE_KIND=mapproxy
APP_WAYBACK_TILE_CACHE_SERVICE_URL=http://127.0.0.1:8080
```

The current app still downloads through the Wayback WMTS template. The service variables document the supported deployment path and are intentionally off by default until a site-specific proxy config is provided.

Minimal MapProxy shape:

```yaml
services:
  demo:
  wmts:

layers:
  - name: wayback
    title: ESRI Wayback
    sources: [wayback_cache]

caches:
  wayback_cache:
    grids: [webmercator]
    sources: [wayback_wmts]

sources:
  wayback_wmts:
    type: tile
    grid: webmercator
    url: https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/%(release)s/%(z)s/%(y)s/%(x)s
```

Treat the example as a starting point; actual release routing depends on how the proxy receives the release id.
