# Real Project Export Validation

Project: `temporal-medium-bouznika-fix-9318797a`

Manifest:

- Path: `/Users/tahaelouali/Developer/Building_change_app/backend/runtime_cache/temporal_projects/temporal-medium-bouznika-fix-9318797a/exports/export_artifact_manifest.json`
- Version: `temporal-results-export-artifact-manifest-v1`
- Entries: 12
- Fingerprint: `0ec41e05145fcee034d091374c3026ad79a9a7ff961c74126e1cae9e9eeda948`

Timings:

- `geojson` first custom export: 12222.6 ms, 9462288 bytes, `results.geojson.custom-884a673367c7`
- `geojson` repeated custom export: 2.25 ms, 9462288 bytes, `results.geojson.custom-884a673367c7`
- `shapefile` first custom export: 29557.13 ms, 195542028 bytes, `results_shapefile.zip.custom-884a673367c7`, qix=13, qgz=1
- `shapefile` repeated custom export: 7.53 ms, 195542028 bytes, `results_shapefile.zip.custom-884a673367c7`, qix=13, qgz=1

Post-fix cache-hit scope log validation:

- `geojson` cache hit after log fix: 3.17 ms, 9462288 bytes
- `shapefile` cache hit after log fix: 1.54 ms, 195542028 bytes
- `scopeType=custom_geometry source=fast_cache` count after fix: 2

Observed counters from the full real export run:

- `EXPORT_FAST_CACHE_HIT`: 2
- `EXPORT_FULL_PROJECT_LOAD_SKIPPED`: 4
- `EXPORT_LIGHTWEIGHT_MANIFEST_LOAD_DONE`: 2
- `EXPORT_SPATIAL_PREFILTER_DONE`: 60
- `EXPORT_SHAPEFILE_SPATIAL_INDEX_CREATED`: 13
- `EXPORT_SHAPEFILE_SPATIAL_INDEX_SKIPPED`: 0

Raw evidence:

- `artifacts/fixes/export_fast_manifest_spatial_cache_aoi_release/13_real_project_export_validation.json`
- `artifacts/fixes/export_fast_manifest_spatial_cache_aoi_release/13_real_project_export_validation.log`
- `artifacts/fixes/export_fast_manifest_spatial_cache_aoi_release/13_real_project_export_validation_cache_hit_after_scope_log_fix.log`

