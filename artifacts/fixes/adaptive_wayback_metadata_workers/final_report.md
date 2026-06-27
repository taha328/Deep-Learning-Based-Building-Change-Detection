# Adaptive Wayback Metadata Workers Final Report

## Problem

Large temporal AOIs can generate tens of thousands of Wayback tilemap metadata checks. A fixed 10-worker preflight can create retry storms when ArcGIS/Wayback or the local network is unstable.

## Root cause

The previous scheduler submitted every candidate tile to one `ThreadPoolExecutor` at a single worker count. Once started, the preflight could not reduce concurrency without restarting the entire job.

## Adaptive policy

Adaptive mode is enabled by default. It starts at 10 workers, observes each window, and downshifts by 2 workers when retry-like instability is detected:

```text
10 -> 8 -> 6 -> 4
```

The minimum defaults to 4 and is not crossed. Fixed rollback is available with `APP_WAYBACK_METADATA_WORKERS_ADAPTIVE_ENABLED=false`.

## Files changed

- `backend/src/config.py`
- `backend/src/domain/wayback.py`
- `backend/src/services/processing.py`
- `backend/tests/test_wayback.py`
- `backend/tests/test_wayback_tile_preflight_cache_locking.py`
- `backend/tests/test_wayback_tile_preflight_cache.py`
- `backend/tests/test_release_resolution_timing.py`
- `README.md`
- `docs/wayback_tile_ingestion.md`
- `.env.example`
- `backend/.env.example`
- `deploy/.env.example`

## Tests

- Targeted Wayback/release tests: `34 passed`.
- Compile check: passed.
- Full backend suite: first run had one non-reproducing mosaic-cache failure outside this change; isolated rerun of that test passed; second full suite passed with `568 passed, 5 skipped`.

## Validation evidence

Mocked stable windows stay at 10 workers. Mocked repeated connection/OSError instability downshifts through `10 -> 8 -> 6 -> 4`, does not retry successful checks, preserves available tile output, and increments failed check count.

## Rollback

Set:

```env
APP_WAYBACK_METADATA_WORKERS_ADAPTIVE_ENABLED=false
APP_WAYBACK_METADATA_WORKERS=10
```

For very unstable networks, use fixed mode with a lower value such as `APP_WAYBACK_METADATA_WORKERS=4`.

## Remaining risks

Retry internals from urllib3 are not always exposed directly. The scheduler therefore counts final visible exceptions and retry-like exception text at the tile-check boundary. Real production logs should be monitored after the next large AOI run to tune the window threshold if the service remains unstable.
