# Casa Resume Execution

Direct resume:

- Project: `temporal-casa-mqwqi0mf-7e7dwl`
- Job id used: `codex-direct-resume-casa-large-result`
- Run request threshold: `change_threshold=0.3`
- `WB_2025_R03` was reused via `TEMPORAL_PAIR_REUSE_FILE_BACKED_ARTIFACT`.
- The run continued to `WB_2025_R03 -> WB_2026_R05`.
- Wayback preflight completed for 55,704 tiles with no failed checks.
- Tiled inference completed 6,160/6,160 tiles and reported 80,130 detections.
- Publication completed successfully and `WB_2026_R05` became complete.

Post-run refresh:

- Generated missing `WB_2025_R03` 10m/15m/20m buffer artifacts from file-backed additions.
- Reused both completed pairs; no inference rerun occurred.

Important limitation:

- Before the full-artifact promotion fix, the 2026 request cleanup deleted the full GeoJSONL stream. The persisted `WB_2026_R05/additions.geojson` currently contains 25,000 features while the inference metadata reports 80,130 detections. Repairing that exact artifact requires rerunning the 2025->2026 pair or recovering the deleted GeoJSONL externally.
