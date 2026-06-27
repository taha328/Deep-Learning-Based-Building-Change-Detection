# BigTIFF-Safe Large Raster Design

## Creation Options

Wayback intermediate mosaics:

```text
driver=GTiff
BIGTIFF=YES
tiled=True
compress=LZW
predictor=2
blockxsize=512 when dimensions allow
blockysize=512 when dimensions allow
```

Reference imagery COG-style outputs:

```text
driver=GTiff
BIGTIFF=YES if estimated uncompressed size >= 4 GiB
BIGTIFF=IF_SAFER otherwise
tiled=True
compress=DEFLATE
predictor=2
interleave=pixel
blockxsize=256
blockysize=256
```

The 256-pixel reference block size preserves the existing reference tile renderer assumptions.

## Atomic Write and Validation

- Write TIFF outputs to `*.partial` or existing temp paths first.
- Close the writer context before validation.
- Reopen the temp output, verify dimensions/bands/CRS, and read a 1-pixel window.
- Rename/replace only after validation succeeds.
- On write or validation failure, remove only the temp/partial file and preserve any previous valid final output.

## Cache Safety

- Wayback mosaic cache hits now require `mosaic.tif` and `valid_mask.tif` to reopen and pass basic validation.
- Canonical reference imagery cache hits now require the COG to reopen as a readable 4-band raster before metadata can make it reusable.
- BigTIFF options do not change cache keys because they do not change geospatial/image semantics.
