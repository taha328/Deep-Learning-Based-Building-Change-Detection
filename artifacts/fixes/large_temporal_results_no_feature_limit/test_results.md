# Test Results

- `backend/.venv/bin/python -m py_compile backend/src/services/processing.py backend/src/services/temporal_projects.py backend/src/schemas.py backend/src/domain/vectorize.py`: passed.
- Focused backend suite: 25 passed, 1 GDAL warning.
- `backend/.venv/bin/python -m pytest backend/tests -q`: passed, 577 passed, 5 skipped, 4 warnings.
- `npm test -- --test-reporter=spec` in `frontend`: passed, 118 tests.
- `npm run build` in `frontend`: passed, Vite chunk-size warning only.

One full backend run first exposed an order-sensitive mosaic test failure; that test passed in isolation and the full suite passed on rerun.
