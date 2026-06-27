# Adaptive Wayback Metadata Workers Inspection

## Code path

- `PREFLIGHT_WORKERS_EFFECTIVE` is logged in `backend/src/services/processing.py` inside `_preflight_release_tile_availability_for_request()` / `_live_preflight()`.
- `PREFLIGHT_REMOTE_START` is emitted in the same `_live_preflight()` block immediately before session setup and the remote tilemap availability call.
- `candidateTileCount` is computed in `_candidate_tile_count()` in `backend/src/services/processing.py` by calling `tile_range_for_bbox(aoi_bbox, zoom)` and multiplying the x/y tile span.
- Tilemap requests are scheduled in `backend/src/domain/wayback.py` by `preflight_wayback_tile_availability()` using `ThreadPoolExecutor` and `as_completed()`.
- Before this change, all candidate tiles were submitted to a single executor, so worker count could not change within a preflight run. The implementation now processes observation windows so the remaining unchecked tiles can downshift without restarting the job.
- Retry/connection failures are observable at the tile-check future boundary as final exceptions from `session.get()`, `raise_for_status()`, urllib3/requests retry exhaustion, or socket-level `OSError`.
- `failedCheckCount` is computed in `preflight_wayback_tile_availability()` from final failed tilemap checks. A nonzero failed count still makes `preflight_complete` false.
- The preflight cache is written in `backend/src/services/processing.py` by `_write_cache_with_short_lock()` after live preflight completion and after rechecking for a cache race.
- The preflight cache key is built in `backend/src/domain/wayback_tile_preflight_cache.py`. It includes release, service URL, tile matrix set, zoom, AOI, and tile range. It does not include worker count, so adaptive downshift does not change cache identity.

## Existing behavior preserved

- Fixed mode is still available by setting `APP_WAYBACK_METADATA_WORKERS_ADAPTIVE_ENABLED=false`.
- In fixed mode, `APP_WAYBACK_TILEMAP_PREFLIGHT_WORKERS` still overrides `APP_WAYBACK_METADATA_WORKERS`; otherwise the fixed worker count defaults to `APP_WAYBACK_METADATA_WORKERS`.
- Cache read, cache lock, cache write, and cache race handling remain in the service layer.
- Inference/model/BANDON behavior is untouched.
