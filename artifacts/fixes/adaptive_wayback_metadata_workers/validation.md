# Adaptive Wayback Metadata Workers Validation

## Controlled validation

Validation used mocked tilemap checks rather than a full expensive temporal inference run.

Evidence:

- Stable mocked tilemap responses across 20 candidate tiles keep final workers at 10 and log `PREFLIGHT_WORKERS_STABLE`.
- Repeated mocked connection instability across 40 candidate tiles downshifts `10 -> 8 -> 6 -> 4`, logs each `PREFLIGHT_WORKERS_DOWNSHIFT`, and logs `PREFLIGHT_WORKERS_MIN_REACHED` at 4.
- The downshift test records every tile id attempted and verifies each candidate tile is checked exactly once.
- The output remains correct after downshift: 32 available tiles, 8 failed checks, and `preflight_complete=false` for 40 mocked candidates.
- Fixed rollback mode passes `adaptive_enabled=false` into the domain preflight and uses the existing fixed worker count.
- Service-layer logging reports `PREFLIGHT_ADAPTIVE_POLICY`, `PREFLIGHT_WORKERS_EFFECTIVE source=adaptive_initial`, and `PREFLIGHT_REMOTE_COMPLETE finalWorkers=... downshiftCount=...`.

## Cache validation

The implementation did not add worker count to the preflight cache key. Existing cache tests still pass, including cache hit on second lookup and cache race recheck before write.

## Runtime validation note

No current large AOI job was mutated for validation. The prompt allowed controlled mocked validation, and the full backend suite passed on rerun.
