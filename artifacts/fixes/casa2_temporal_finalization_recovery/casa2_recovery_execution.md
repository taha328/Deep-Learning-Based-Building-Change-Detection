# Casa 2 Recovery Execution

Project: `temporal-casa-2-mqw5jysv-ri6q84`
Completed request reused: `43a37e757f6111e5929acc40`
Original job: `job-aa092d18d777402994b8e8fe24b59893`
Original Celery task: `27f5aeb4-fdf0-4151-84eb-5c656ecffd56`
Recovered pair: `WB_2024_R02 -> WB_2025_R03`

## Actions Taken

1. Preserved request outputs and diagnostics before changing state.
2. Confirmed the original worker was alive but stuck in CPU-bound GEOS buffer/noding work after request-level success.
3. Stopped the dev stack and removed only the orphaned Redis delivery tag for the original Casa 2 task.
4. Reused the completed request output instead of rerunning inference.
5. Published the request into milestone `WB_2025_R03` with summary-backed finalization to avoid regenerating oversized derived geometry.
6. Refreshed project sidecars after repository persistence so compact loading sees current metrics and artifacts.
7. Marked the original job terminal `failed` with `error_code=temporal_partial_recovery`, because the original two-pair temporal job did not run every required pair.

## Recovered Artifacts

- `additions.geojson`: 15528 features
- `building_change_buffer_10m.geojson`: 10483 features
- `building_change_buffer_15m.geojson`: 10483 features
- `building_change_buffer_20m.geojson`: 10483 features
- `automated_building_blocks.geojson`: empty placeholder, not exposed as an existing compact artifact

## Preserved Request Outputs

The original completed request outputs were not deleted. The preserved request directory still contains the large inference outputs including `prediction_change_probability.tif`, `prediction_change_mask.tif`, `prediction_change_polygons.geojsonl`, `building_change_polygons.geojson`, `run_response.json`, `manifest.json`, `timing.json`, and `tiled_inference_metadata.json`.

## Recovery Result

Recovery execution details are stored in `casa2_recovery_execution.json`.
After-state database evidence is stored in `db_after_snapshot.json`.
Public API after-state evidence is stored in `api_job_after.json` and `api_compact_project_after.json`.
