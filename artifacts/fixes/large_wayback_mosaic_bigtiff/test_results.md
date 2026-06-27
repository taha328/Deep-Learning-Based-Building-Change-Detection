# Large Wayback Mosaic BigTIFF Test Results

## Commands

```bash
backend/.venv/bin/python -m pytest backend/tests/test_mosaic.py backend/tests/test_temporal_reference_imagery.py backend/tests/test_inference_reference_imagery_reuse.py -q
```

Result: `80 passed, 4 warnings in 5.93s`.

```bash
backend/.venv/bin/python -m py_compile backend/src/config.py backend/src/domain/*.py backend/src/services/*.py backend/src/jobs/tasks.py
```

Result: passed.

```bash
backend/.venv/bin/python -m pytest backend/tests -q
```

Result: `574 passed, 5 skipped, 4 warnings in 35.19s`.

## Regression Coverage

- Large Wayback mosaic creation options force `BIGTIFF=YES`.
- Large final raster estimates select `BIGTIFF=YES`; small estimates select `BIGTIFF=IF_SAFER`.
- GTiff outputs are tiled, compressed, and use block size options.
- Exact `TIFFAppendToStrip: Maximum TIFF file size exceeded. Use BIGTIFF=YES creation option.` failure is covered.
- Failed mosaic write removes `mosaic.tif.partial` and does not publish `mosaic.tif` or metadata.
- Validation failure removes the partial mosaic and does not publish metadata.
- Corrupt cached `mosaic.tif` is not reused and triggers a rebuild.
- Reference COG validation failure removes the temp file and does not replace the final COG.
