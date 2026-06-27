# Before State

## Worktree

- Branch: `codex/inference-persistent-runner-benchmarks`
- HEAD: `d2af39da26d0caf53e476eca1f6132c72f619986`
- Existing unrelated worktree changes were present before this fix, including many deleted files under `artifacts/benchmarks/`, plus untracked `artifacts/diagnostics/` and `artifacts/ops/`.
- This fix does not restore, delete, or otherwise modify those unrelated benchmark artifacts.

## Preserved Evidence

- Copied previous inspection to `diagnostics_before/`.
- API before snapshots:
  - `api_job_before.json`
  - `api_compact_project_before.json`
- DB before snapshot:
  - `db_before_snapshot.json`
- Redis/process before snapshots:
  - `process_snapshot_before.txt`
  - `redis_snapshot_before.txt`
  - `worker_liveness_30s.txt`
  - `worker_70726_sample.txt`

## Casa 2 State Before Fix

- Project: `temporal-casa-2-mqw5jysv-ri6q84`
- Job: `job-aa092d18d777402994b8e8fe24b59893`
- Completed request: `43a37e757f6111e5929acc40`
- Job state: `running`
- Job stage: `saving_artifacts`
- Job progress: `92`
- Job message: `Completed`
- Job `completed_at`: `null`
- Job `result_run_id`: `null`

Compact project before recovery:

- Top-level status: `pending`
- Top-level `complete_milestone_count`: `2`
- Milestone entries: all `pending`
- Milestone metrics: all `null`
- Milestone artifacts: all empty

DB before recovery:

- Milestone rows: all pending
- Metric rows for Casa 2 milestones: none
- Artifact rows for Casa 2: `0`
- Run rows for Casa 2: `0`
- Run row for request `43a37e757f6111e5929acc40`: none

Request output before recovery:

- `run_response.json` exists and reports success.
- `prediction_change_probability.tif` exists.
- `prediction_change_mask.tif` exists.
- `prediction_change_polygons.geojsonl` exists.
- `building_change_polygons.geojson` exists.
- `manifest.json`, `timing.json`, and `tiled_inference_metadata.json` exist.

## Safety Notes

- The 6090-tile inference was not rerun.
- No request output files were deleted.
- Redis was not flushed.
- Only the orphaned Casa 2 unacked delivery tag was removed after the stale worker was stopped and the tag payload was confirmed to contain the Casa 2 job id, task id, and project id.
