# Adaptive Wayback Metadata Workers Test Results

## Commands

```bash
backend/.venv/bin/python -m pytest backend/tests/test_wayback.py backend/tests/test_wayback_tile_preflight_cache_locking.py backend/tests/test_wayback_tile_preflight_cache.py backend/tests/test_release_resolution_timing.py -q
```

Result: `34 passed in 1.51s`.

```bash
backend/.venv/bin/python -m py_compile backend/src/config.py backend/src/services/*.py backend/src/domain/*.py backend/src/jobs/tasks.py
```

Result: passed.

```bash
backend/.venv/bin/python -m pytest backend/tests -q
```

First full-suite result: `1 failed, 567 passed, 5 skipped`. The failed test was `backend/tests/test_mosaic.py::test_download_wayback_mosaic_does_not_reuse_nonreusable_transient_cache_entry`, outside the adaptive metadata preflight path.

Isolation check:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_mosaic.py::test_download_wayback_mosaic_does_not_reuse_nonreusable_transient_cache_entry -q
```

Result: `1 passed in 1.02s`.

Second full-suite run:

```bash
backend/.venv/bin/python -m pytest backend/tests -q
```

Result: `568 passed, 5 skipped, 4 warnings in 33.75s`.

## Coverage added

- Adaptive mode starts at configured initial workers.
- Stable windows remain at 10 workers.
- Repeated instability downshifts `10 -> 8 -> 6 -> 4`.
- Minimum worker floor is respected.
- Successful checks are not repeated after downshift.
- Available tile count and failed check count remain correct across downshift.
- Fixed rollback mode uses existing fixed worker settings and does not enable adaptive downshift.
- Invalid adaptive worker config is clamped and logged.
- Logs include policy, stable/downshift decisions, minimum reached, and final worker count.
