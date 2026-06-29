# Final Release Validation Report

Date: 2026-06-29

Release: `v0.1.5`

## Result

Accepted.

The release-safe Docker env defaults were synced into the published release package, public Mapbox runtime config is available to the installed frontend, GHCR images were rebuilt and published, the GitHub release package was rebuilt and uploaded, and the public raw installer completed a Docker CPU validation from install through health, runtime, smoke, temporal jobs, frontend basemap, result layer, exports, and repeated export cache hit.

## Release Artifacts

- GitHub release: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/releases/tag/v0.1.5`
- Source commit: `b346acb4d7e66e979de5707269fca231027890ef`
- Tag object: `00ed53c86c333a4c197b9a26853b2d023190e22e`
- Frontend image: `ghcr.io/taha328/building-change-frontend:v0.1.5`
- Frontend digest: `sha256:9a0c217db3f79a785a98c7ea07e2d6adf97df7bfc6800e81f3d6775a158b9481`
- Backend CPU image: `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`
- Backend CPU digest: `sha256:05818cf6088ce485e55a3918b5f9d8131cd4f1f3041f150420b7d4cafbf18fac`
- Release package: `building-change-app.zip`
- Release package digest: `sha256:fa89608fcfa2a70d7ed01052b5733c37d5b1c0031032f0f169aa4130fe09bf4a`

## Raw Installer

Command:

```sh
curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash
```

Installed to:

`/Users/tahaelouali/.local/share/building-change-app/releases/20260629T130818Z.22gBGj/building-change-app`

Validation:

- containers started automatically
- PostGIS migration passed
- Redis healthy
- backend API healthy
- frontend healthy
- Celery worker running
- `./scripts/health.sh` passed
- `./scripts/validate-runtime.sh` passed with `device_resolved=cpu`
- `./scripts/smoke-test.sh` passed

## Full Pipeline

Smoke detection:

- job id: `job-aad23e11a3e7467394c1865309a9577e`
- request hash: `25d8e23d09315e8fd2cdc44b`
- artifact count: `14`

Temporal non-empty export project:

- project id: `release-a2z-bouskoura-20260629`
- job id: `job-d954dc5e24e6492eba8ddf98e367ac17`
- pair request hash: `23023e30a328b4c9a29240d7`
- target additions feature count: `25`
- target artifact count: `5`

Exports:

- whole-project GeoJSON: valid, `967817` bytes, `100` features
- whole-project Shapefile/QGIS ZIP: valid, `4295931` bytes, `.shp`, `.dbf`, and `.qgz` present
- custom AOI GeoJSON: valid, `786648` bytes, repeated hash match, cache hit
- custom AOI Shapefile/QGIS ZIP: valid, `1956476` bytes, repeated hash match, cache hit

Explicit backend cache logs include `EXPORT_FAST_CACHE_HIT` for both repeated custom GeoJSON and shapefile exports.

## Frontend

Mapbox runtime config is served by the installed frontend. Browser evidence shows satellite imagery, Mapbox attribution, completed project result controls, and result overlay over the basemap. No Mapbox token errors were recorded.

Screenshots:

- `frontend_mapbox_basemap_default.png`
- `frontend_bouskoura_results_over_basemap.png`

## Evidence Index

- `release_env_parameter_verification.md`
- `release_build_evidence.md`
- `raw_installer_validation.md`
- `docker_cpu_pipeline_validation.md`
- `full_a_to_z_pipeline_validation.md`
- `frontend_mapbox_basemap_evidence.md`
- `cleanup_evidence.md`
- `bouskoura_export_validation_summary.json`
- `frontend_mapbox_default_evidence.json`
- `frontend_bouskoura_result_evidence.json`
- `exports/`

## Cleanup

`./scripts/stop.sh` was run from the installed bundle after validation. `docker ps` showed no running containers afterward.
