# Casa 2 Indicators Unavailable Inspection

## Scope

Read-only investigation of why Casa 2 shows the unavailable indicators message:

- Project: `temporal-casa-2-mqw5jysv-ri6q84`
- Job: `job-aa092d18d777402994b8e8fe24b59893`
- Request/run hash: `43a37e757f6111e5929acc40`

No code, database rows, Redis keys, runtime-cache data, or project state were modified. The only files written were diagnostics under this directory.

## High-Level Finding

The request-level tiled inference completed successfully and produced the expected outputs. Casa 2 still shows indicators unavailable because the completed request has not been persisted into the temporal project as a completed milestone with metrics/artifacts.

At inspection time, the compact project API, database rows, and project files all agreed:

- all milestones were `pending`
- all milestone `metrics` values were absent
- all milestone `pair_request_hash` and `populated_request_hash` values were absent
- project artifact count was `0`
- project run count was `0`
- the job was still `running` at `saving_artifacts`

## API Snapshot Summary

### Compact Project

Snapshot file: `api_snapshot_compact_project.json`

- `project_id`: `temporal-casa-2-mqw5jysv-ri6q84`
- `name`: `CASA 2`
- `project_dir`: `/Users/tahaelouali/Developer/Building_change_app/backend/runtime_cache/temporal_projects/temporal-casa-2-mqw5jysv-ri6q84`
- `milestone_count`: `3`
- `complete_milestone_count`: `2`
- `updated_at`: `2026-06-27T09:23:37Z`

Note: the compact API's top-level `complete_milestone_count` is `2`, but each milestone entry still has `status: pending`, `metrics: null`, and empty `artifacts`. The UI's indicator cards depend on milestone-level metrics, so this top-level count does not make spatial indicators available.

Milestones:

| Milestone | Status | Metrics | Artifacts | Pair request hash |
| --- | --- | --- | --- | --- |
| `WB_2024_R02` | `pending` | `None` | `{}` | `None` |
| `WB_2025_R03` | `pending` | `None` | `{}` | `None` |
| `WB_2026_R05` | `pending` | `None` | `{}` | `None` |

### Job Status

Snapshot file: `api_snapshot_job_status.json`

- `status`: `running`
- `stage`: `saving_artifacts`
- `progress`: `92`
- `message`: `Completed`
- `celery_task_id`: `27f5aeb4-fdf0-4151-84eb-5c656ecffd56`
- `completed_at`: `None`
- `result_run_id`: `None`
- `error_code`: `None`
- `error_message`: `None`

## Request Output Summary

Inventory file: `request_output_inventory.txt`

Request directory:

`/Users/tahaelouali/Developer/Building_change_app/backend/runtime_cache/requests/43a37e757f6111e5929acc40`

Expected outputs are present:

- `prediction_change_probability.tif` (~13 GB)
- `prediction_change_mask.tif` (~7 MB)
- `prediction_change_polygons.geojsonl` (~57 MB)
- `building_change_polygons.geojson` (~134 MB)
- `run_response.json` (~148 MB)
- `manifest.json` (~35 MB)
- `tiled_inference_metadata.json`
- `timing.json`

Request metadata:

- `run_response.json`: `success: true`
- `processed_tiles`: `6090`
- `total_tiles`: `6090`
- `feature_count`: `15528`
- `timing.json` completed at `2026-06-27T13:43:45.763679Z`

## Project Artifact Summary

Inventory file: `project_artifact_inventory.txt`

Project directory:

`/Users/tahaelouali/Developer/Building_change_app/backend/runtime_cache/temporal_projects/temporal-casa-2-mqw5jysv-ri6q84`

The project directory contains project metadata and milestone reference imagery, but no generated temporal result artifacts for the completed request. The inspected project files did not contain request hash `43a37e757f6111e5929acc40`.

Observed project metadata state:

- `project.json`: all milestones pending, no metrics/artifacts/request hash
- `project_summary.json`: `complete_milestone_count: 0`
- `project_compact_metadata.json`: all milestones pending, no metrics/artifacts/request hash
- `project_manifest.json`: no request hash

## Frontend Explanation

The frontend shows the unavailable indicators state because milestone metrics are absent.

Relevant code paths:

- `frontend/src/features/temporal/MilestoneMetricCards.tsx`
  - `hasMetrics(milestone)` checks `Boolean(milestone.metrics)`.
  - The unavailable message is rendered when the selected milestone has no `metrics`.
  - Temporal charts only include milestones where `status === "complete" && metrics` exists.

- `frontend/src/features/temporal/TemporalMosaicPanel.tsx`
  - Completed temporal result logic also requires at least one milestone with `status === "complete" && milestone.metrics`.

Given the current API payload, the frontend behavior is expected.

## Backend Explanation

The expected backend transition after request completion is to apply the pair response to a temporal milestone, recompute project outputs, refresh the project bundle, and save the project. The inspected state shows that transition has not completed or has not persisted.

Relevant code paths:

- `backend/src/services/temporal_projects.py`
  - `run_temporal_project`
  - `_apply_pair_response_to_milestone`
  - `_recompute_project_outputs_from_index`
  - `_refresh_project_bundle`
  - `_save_project`
  - `publish_completed_tiled_request`

- `backend/src/jobs/tasks.py`
  - temporal job task publishes `saving_artifacts`, then should persist completion after a successful response.

The current job status is still `running` at `saving_artifacts`, so the missing state may be due to an in-progress or stuck finalization phase rather than a completed job with bad UI rendering.

## Conclusion

This is not primarily a frontend display problem and not a missing request-output problem. The root issue is that successful request output `43a37e757f6111e5929acc40` is not yet connected to Casa 2 temporal project state. Until a milestone has persisted metrics and a completed status, the compact project endpoint will continue to serve no indicators, and the UI will continue to show the unavailable indicators message.

See also:

- `timeline.md`
- `db_snapshot_readonly.md`
- `root_cause_matrix.md`
- `next_steps_no_code_change.md`
