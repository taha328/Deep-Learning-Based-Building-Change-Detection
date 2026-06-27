# Large Wayback Mosaic BigTIFF Final Report

## Problem

Large Wayback AOIs can exceed the classic TIFF 4 GiB limit during `mosaic.tif` creation, producing `TIFFAppendToStrip: Maximum TIFF file size exceeded`.

## GDAL Root Cause

The failing writer used compressed tiled GeoTIFF output without explicit `BIGTIFF`. With compression, GDAL cannot always predict final size and may choose classic TIFF until libtiff overflows.

## Solution Design

- Added shared raster write helpers for size estimation, BigTIFF policy, tiled options, and reopen/read validation.
- Forced `BIGTIFF=YES` for Wayback mosaic and valid-mask GeoTIFFs.
- Kept final reference COG-style outputs explicit: `BIGTIFF=YES` for estimated outputs >= 4 GiB, otherwise `IF_SAFER`.
- Wrote mosaic TIFFs through `.partial` files and validated before rename.
- Hardened cache reuse so corrupt mosaic/reference TIFFs are not accepted.

## Files Changed

- `backend/src/domain/raster_write_options.py`
- `backend/src/domain/mosaic.py`
- `backend/src/domain/reference_imagery_cache.py`
- `backend/src/services/temporal_reference_imagery.py`
- `backend/tests/test_mosaic.py`
- `backend/tests/test_temporal_reference_imagery.py`
- `README.md`
- `docs/wayback_tile_ingestion.md`

## Creation Options Used

Wayback mosaics: `BIGTIFF=YES`, `tiled=True`, `compress=LZW`, `predictor=2`, block size up to 512.

Reference COG-style outputs: `compress=DEFLATE`, `predictor=2`, `interleave=pixel`, 256-pixel blocks, `BIGTIFF=YES` for large estimates and `IF_SAFER` otherwise.

## Tests and Validation

- Targeted raster/mosaic/reference tests: `80 passed`.
- Compile check: passed.
- Full backend suite: `574 passed, 5 skipped`.
- Controlled tests cover option selection, the exact TIFF overflow error string, partial cleanup, validation failure, corrupt cache rejection, and reference COG temp cleanup.

## Current Casa City Job Recovery Status

The Casa City job was still running at the latest check and had not reached a failed mosaic state. No runtime cache or project output was deleted. Because the active Celery worker predates this code change, a retry should be run only after the worker is restarted with this commit.

## Rollback Plan

Rollback is a code revert. There are no new environment variables and no cache-key changes.

## Remaining Risks

The code still builds the RGB mosaic in memory before writing. This fixes the classic TIFF size overflow, but extremely large AOIs can still be constrained by RAM or disk capacity. The next large production retry should be watched for `WAYBACK_MOSAIC_SIZE_ESTIMATE`, `WAYBACK_MOSAIC_GTIFF_OPTIONS`, `WAYBACK_MOSAIC_WRITE_DONE`, and `WAYBACK_MOSAIC_VALIDATE_DONE`.
