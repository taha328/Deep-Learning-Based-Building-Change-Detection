# Adaptive Wayback Metadata Workers Design

## Policy

Default adaptive policy:

```text
initial workers: 10
minimum workers: 4
step: 2
ladder: 10 -> 8 -> 6 -> 4
window size: 1000 candidate tile checks
```

Adaptive mode is controlled by:

- `APP_WAYBACK_METADATA_WORKERS_ADAPTIVE_ENABLED=true`
- `APP_WAYBACK_METADATA_WORKERS_INITIAL=10`
- `APP_WAYBACK_METADATA_WORKERS_MIN=4`
- `APP_WAYBACK_METADATA_WORKERS_STEP=2`

Rollback/fixed mode:

- `APP_WAYBACK_METADATA_WORKERS_ADAPTIVE_ENABLED=false`
- `APP_WAYBACK_METADATA_WORKERS=<fixed worker count>`

The older `APP_WAYBACK_TILEMAP_PREFLIGHT_WORKERS` remains honored in fixed mode for targeted tilemap-preflight rollback.

## Scheduler

`preflight_wayback_tile_availability()` now processes candidate tiles in bounded observation windows. Each window creates a `ThreadPoolExecutor` at the current worker count. Completed successful checks are recorded immediately and never resubmitted. After a window finishes, the scheduler evaluates instability and applies the next worker count only to the remaining unchecked windows.

Per-window metrics:

- attempted checks
- successful checks
- final failed checks
- connection-like errors
- timeout errors
- retry-exhausted errors when observable
- elapsed milliseconds

Downshift reasons:

- `retry_exhausted`: any final exception text indicating exhausted urllib3 retry state or max retries.
- `timeout_instability`: at least two timeouts in one window, or timeout rate above 2% for windows with at least 100 attempts.
- `connection_instability`: at least two connection-like/socket errors in one window, or connection error rate above 2% for windows with at least 100 attempts.

The scheduler never downshifts below the configured minimum. If a window remains unstable at the minimum, it logs `PREFLIGHT_WORKERS_MIN_REACHED` and continues checking remaining tiles at the floor.

## Output and cache semantics

The available tile set, failed check count, missing count, and `preflight_complete` semantics remain deterministic for a given set of tilemap responses. Final failed checks are still counted; they are not swallowed. The preflight cache is written only after the live preflight returns through the existing service-layer cache write path.
