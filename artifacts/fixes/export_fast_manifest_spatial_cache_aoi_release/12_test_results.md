# Test Results

- `npm --prefix frontend test -- src/features/map/map-drawing.test.ts`: passed, 128 tests.
- `npm --prefix frontend run build`: passed (`tsc -b && vite build`). Vite reported existing large chunk warnings only.
- `backend/.venv/bin/python -m pytest -q backend/tests/test_temporal_results_exports.py`: passed, 16 tests, 1 GDAL warning.
- `backend/.venv/bin/python -m pytest -q backend/tests`: passed, 582 tests, 5 skipped, 4 warnings.

New coverage:

- Custom export fast-cache hit uses the lightweight manifest and skips project hydration.
- AOI overlay parent/child visibility helpers enforce hidden, result-viewing, and custom-zone workflow states.

