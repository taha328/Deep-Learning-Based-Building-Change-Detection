# Test Results

Targeted checks already completed:

```text
rtk backend/.venv/bin/python -m py_compile backend/src/services/temporal_exports.py backend/src/services/temporal_projects.py backend/src/api/routes/temporal_projects.py
passed
```

```text
rtk backend/.venv/bin/python -m pytest backend/tests/test_temporal_results_exports.py -q
14 passed in 2.55s
```

Added regression coverage:

- Forces file-backed behavior by lowering `TEMPORAL_VECTOR_TILE_METADATA_THRESHOLD_BYTES`.
- Creates completed temporal milestones whose additions and buffer layers exist only as on-disk GeoJSON artifacts.
- Exports all supported formats: `xlsx`, `kml`, `geojson`, `topojson`, `json`, `tsv`, and `shapefile`.
- Validates non-empty exported data, shapefile layer names, no `EXPORT_LAYER_MISSING`, and custom GeoJSON cache reuse with unchanged mtime.

Full-suite verification:

```text
rtk backend/.venv/bin/python -m pytest backend/tests -q
578 passed, 5 skipped, 4 warnings in 37.74s
```

```text
rtk npm test --prefix frontend
118 passed in 318.974542 ms
```

```text
rtk npm run build --prefix frontend
passed
```

The frontend build retained the existing Vite large-chunk warning.

```text
rtk backend/.venv/bin/python -m py_compile backend/src/services/temporal_exports.py backend/src/services/temporal_projects.py backend/src/api/routes/temporal_projects.py
passed
```

```text
rtk git diff --check
passed
```

```text
rtk git status --short
completed; intended source/test/report files are modified or untracked, with pre-existing unrelated benchmark deletions and diagnostics/ops directories left unstaged.
```
