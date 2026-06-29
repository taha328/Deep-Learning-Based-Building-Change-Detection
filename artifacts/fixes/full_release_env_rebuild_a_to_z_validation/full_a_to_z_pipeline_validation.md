# Full A-to-Z Pipeline Validation

Date: 2026-06-29

Raw-installed Docker release target:

`/Users/tahaelouali/.local/share/building-change-app/releases/20260629T130818Z.22gBGj/building-change-app`

## Project and AOI

Project:

- `release-a2z-bouskoura-20260629`
- name: `Release A2Z Bouskoura non-empty export validation`
- AOI: `[-7.536,33.366]` to `[-7.533,33.369]`
- releases: `WB_2025_R03` to `WB_2026_R05`
- estimated tiles: `24`

Run result:

- job id: `job-d954dc5e24e6492eba8ddf98e367ac17`
- status: `completed`
- result run id: `temporal-release-a2z-bouskoura-20260629-195a732cabdb476d9ae870cba0dcd477`
- pair request hash: `23023e30a328b4c9a29240d7`
- complete milestones: `2`
- target milestone artifacts: `5`
- additions feature count: `25`

## Export Files

Detailed machine-readable evidence is in:

`artifacts/fixes/full_release_env_rebuild_a_to_z_validation/bouskoura_export_validation_summary.json`

Downloaded files are in:

`artifacts/fixes/full_release_env_rebuild_a_to_z_validation/exports/`

Whole-project GeoJSON:

- file: `bouskoura_whole_results.geojson`
- size: `967817`
- sha256: `27c5895cb0e1361dc9da2be7fb8a35c618312e22146850ceb11e0466f0c09040`
- feature count: `100`
- geometry type: `Polygon`
- duration: `317.8 ms`

Whole-project Shapefile/QGIS ZIP:

- file: `bouskoura_whole_results.zip`
- size: `4295931`
- sha256: `1bf8e2100a091727d5887ee899afe057e4890e83ef0b24d4f38e1b1c0e17b190`
- ZIP entries: `57`
- contains `.shp`: true
- contains `.dbf`: true
- contains `.qgz`: true
- duration: `508.77 ms`

Custom AOI GeoJSON, first request:

- file: `bouskoura_custom_first_results.geojson`
- size: `786648`
- sha256: `eab9e64e498ee9d19a910e6554b0d8d3dd0802dba523781022fd549b56629818`
- feature count: `100`
- duration: `363.97 ms`

Custom AOI Shapefile/QGIS ZIP, first request:

- file: `bouskoura_custom_first_results.zip`
- size: `1956476`
- sha256: `92e1e465ae6500eca657813ddea5f50f09e151ba7d96c579e3409920c86b9699`
- ZIP entries: `64`
- contains `.shp`: true
- contains `.dbf`: true
- contains `.qgz`: true
- duration: `570.78 ms`

Custom AOI repeated GeoJSON:

- file: `bouskoura_custom_repeat_results.geojson`
- size: `786648`
- sha256: `eab9e64e498ee9d19a910e6554b0d8d3dd0802dba523781022fd549b56629818`
- duration: `9.95 ms`

Custom AOI repeated Shapefile/QGIS ZIP:

- file: `bouskoura_custom_repeat_results.zip`
- size: `1956476`
- sha256: `92e1e465ae6500eca657813ddea5f50f09e151ba7d96c579e3409920c86b9699`
- duration: `14.96 ms`

The repeated custom exports had matching hashes and much lower durations, proving the cache path was used.

## Explicit Cache Log Evidence

```text
EXPORT_FAST_CACHE_HIT projectId=release-a2z-bouskoura-20260629 format=geojson scopeType=custom_geometry path=/data/runtime_cache/temporal_projects/release-a2z-bouskoura-20260629/exports/results.geojson.custom-95c28a1b4445 cacheKey=b7477c17d15ca19e4db94109f8e9eb4a8e6758c13a796dddf80cd3462bd90086 bytes=786648
EXPORT_FAST_CACHE_HIT projectId=release-a2z-bouskoura-20260629 format=shapefile scopeType=custom_geometry path=/data/runtime_cache/temporal_projects/release-a2z-bouskoura-20260629/exports/results_shapefile.zip.custom-95c28a1b4445 cacheKey=985d460a45dc51f97c8ca36cb252a848de1d109f681ce3bc61291a4a144a067f bytes=1956476
```

## Frontend Result Layer Check

Evidence:

- `frontend_bouskoura_results_over_basemap.png`
- `frontend_bouskoura_result_evidence.json`

The loaded frontend showed:

- completed milestone controls
- `Telecharger les resultats` control
- Mapbox attribution
- one map canvas
- result overlay visible over satellite basemap in the screenshot
- no Mapbox token errors in browser console logs
- no yellow inline style candidates for unwanted AOI overlay
