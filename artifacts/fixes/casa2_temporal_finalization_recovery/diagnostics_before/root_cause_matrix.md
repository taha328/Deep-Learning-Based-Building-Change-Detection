# Root Cause Matrix

## Summary

The frontend message is caused by missing temporal milestone metrics in the Casa 2 compact project payload. The request-level inference output exists and is successful, but the temporal project has not registered that output into milestones, metrics, artifacts, or job completion state.

The compact API reports top-level `complete_milestone_count: 2`, while the milestone entries themselves remain `pending` with `metrics: null` and empty `artifacts`. The unavailable-indicators UI is driven by the milestone-level fields, so the top-level count does not change the diagnosis.

## Matrix

| Candidate cause | Evidence for | Evidence against | Assessment |
| --- | --- | --- | --- |
| Frontend-only rendering bug | The message is rendered by frontend components when metrics are absent. | API and DB both show `metrics: None`, milestones `pending`, and no artifacts. The frontend is reflecting backend state. | Not root cause. |
| Request output missing | UI asks for completed milestone indicators. | Request directory exists and contains probability/mask rasters, GeoJSON outputs, manifest, timing, tiled metadata, and successful `run_response.json`. | Rejected. |
| Request inference failed | Job is not completed in temporal job table. | Request timing and response report success; tiles are `6090/6090`; feature count is `15528`; error fields are empty. | Rejected for request-level inference. |
| Export bundle disabled | Code can skip tiled export bundles by default. | Core request outputs exist. Temporal project finalization can consume `run_response.json` and artifacts without this export bundle being the only source of truth. | Not sufficient by itself. |
| Request outputs were written under the wrong request hash | Expected request hash was `43a37e757f6111e5929acc40`. | The expected request directory exists and manifest/run metadata use that same hash. | Rejected. |
| Request output is orphaned from Casa 2 project state | Project compact payload has all milestones pending, empty artifacts, and no metrics. DB milestones have no request hash. Project metadata files do not contain the request hash. DB artifact/run counts are zero. | None found. | Strongly supported. |
| Temporal finalization still running or stuck after request completion | Job remains `running`, stage `saving_artifacts`, progress `92`, no `completed_at`, no `result_run_id`; Celery task is `STARTED`/unacked. | A live worker may still eventually complete; current snapshot cannot distinguish slow finalization from permanently stuck finalization. | Strongly supported as current operational state. |
| Temporal project persistence wrote to DB but not filesystem | DB and filesystem agree: milestones pending, metrics absent, artifact count zero. | No divergence found. | Rejected. |
| Temporal project persistence wrote to filesystem but not DB | Filesystem project JSON/summary/manifest/compact metadata all lack request hash, metrics, and generated artifacts. | No divergence found. | Rejected. |
| UI selected the wrong milestone | All three milestones have no metrics and are `pending`. | Any selected milestone would show unavailable. | Not root cause. |

## Most Likely Root Cause

The Casa 2 request-level tiled inference completed successfully, but the temporal project finalization/publication step did not persist the completed request into the Casa 2 milestone/project state at the time of inspection.

The missing transition is the path that should connect request `43a37e757f6111e5929acc40` to a milestone by setting `pair_request_hash`/`populated_request_hash`, generating temporal metrics/artifacts, refreshing the project bundle, and saving the project. Because that transition has not completed or persisted, the compact project endpoint has no metrics to serve, and the frontend correctly renders the unavailable-indicators state.

## Evidence Chain

1. Request-level inference completed:
   - `run_response.json`: `success: true`
   - `tiled_inference_metadata.json`: `processed_tiles: 6090`, `total_tiles: 6090`, `feature_count: 15528`
   - `timing.json`: completed at `2026-06-27T13:43:45.763679Z`

2. Project-level finalization did not appear in persisted state:
   - API compact project: all milestones `pending`, `metrics: null`, `artifacts: {}`
   - DB milestones: all `pending`, `pair_request_hash: None`, `populated_request_hash: None`
   - DB artifacts: `0`
   - DB runs for project: `0`
   - Project files: no request hash `43a37e757f6111e5929acc40`

3. Job is not completed:
   - Job status: `running`
   - Stage: `saving_artifacts`
   - Progress: `92`
   - `completed_at`: `None`
   - `result_run_id`: `None`

4. Frontend unavailable state is expected from this payload:
   - `MilestoneMetricCards` renders unavailable when `milestone.metrics` is falsy.
   - Chart/completed-result logic only uses milestones where `status === "complete" && metrics` exists.
