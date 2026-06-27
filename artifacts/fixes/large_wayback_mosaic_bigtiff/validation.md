# Large Wayback Mosaic BigTIFF Validation

## Controlled Validation

- Synthetic option tests prove Wayback mosaics force `BIGTIFF=YES`.
- Synthetic size-estimate tests prove final reference output policy selects `BIGTIFF=YES` above 4 GiB and `IF_SAFER` below 4 GiB.
- Mocked GDAL failure test covers the exact classic TIFF overflow message.
- Mocked validation failure test proves a written file that cannot be reopened/read is not published.
- Cache corruption test proves `metadata.json` plus a corrupt `mosaic.tif` is not enough for reuse.
- Full backend suite passed after the change.

## Real-World State

- Current Casa City job `job-f3eeba1c30964ea88481b71bcbe2c458` was still running at the last check.
- API status showed `stage=fetching_imagery`, `processed_tile_count=33095`, `total_tile_count=55704`, `failed_tile_count=0`, `retry_count=0`.
- No `*.partial`, `*.tmp`, `*.tmp.tif`, or lock files were found under `backend/runtime_cache/wayback_mosaics` or the current request/tmp workspaces.
- Disk space at validation time: about 80 GiB available.

## Large AOI Validation Decision

The active job is running in Celery worker PID `67060`, which predates this code change. Restarting the worker or modifying the request/project workspace would mutate an active run. No large AOI rebuild was executed. The safe validation for this turn is the controlled BigTIFF/atomic-write suite plus full backend tests.
