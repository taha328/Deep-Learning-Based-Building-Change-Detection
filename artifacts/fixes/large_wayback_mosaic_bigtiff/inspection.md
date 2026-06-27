# Large Wayback Mosaic BigTIFF Inspection

## Writer Inventory

- Failing `mosaic.tif` writer: `backend/src/domain/mosaic.py`, `_write_cached_mosaic()`.
- Writer API: Rasterio `rasterio.open(..., "w", driver="GTiff")`.
- Previous `mosaic.tif` options: `compress="LZW"`, `tiled=True`, dynamic block sizes, no explicit `BIGTIFF`.
- Previous `valid_mask.tif` options: `compress="LZW"`, `tiled=True`, dynamic block sizes, no explicit `BIGTIFF`.
- Cache publish model: the code writes into a staging directory created with `tempfile.mkdtemp(...)`, then publishes the staging directory to `backend/runtime_cache/wayback_mosaics/<cache_key>`.
- Previous cache reuse check: metadata and file existence were checked, but `mosaic.tif` and `valid_mask.tif` were not reopened/read before reuse.

## Reference Imagery

- `reference_imagery_cog.tif` is generated in `backend/src/services/temporal_reference_imagery.py`, `ensure_reference_imagery_cog()`.
- Writer API: Rasterio `rasterio.open(..., "w", driver="GTiff")`.
- Previous creation options: tiled GTiff, `blockxsize=256`, `blockysize=256`, `compress="DEFLATE"`, `predictor=2`, `interleave="pixel"`, `BIGTIFF="IF_SAFER"`.
- The writer already used a temp path, but did not select `BIGTIFF=YES` for estimated large final outputs and did not reopen/read the temp file before replacing the final COG.
- Overviews are generated with `dst.build_overviews(...)` inside the same output file. The selected BigTIFF policy therefore applies to the base image and its internal overviews.

## Root Reuse Risks

- GDAL can select classic TIFF under compression if size is not explicitly forced, then fail with `TIFFAppendToStrip: Maximum TIFF file size exceeded`.
- A corrupt `mosaic.tif` plus valid-looking `metadata.json` could previously be considered reusable because the cache hit path did not validate the TIFF files.
- Canonical reference COG reuse previously checked file existence/size and metadata, not that the COG reopened as a readable raster.
