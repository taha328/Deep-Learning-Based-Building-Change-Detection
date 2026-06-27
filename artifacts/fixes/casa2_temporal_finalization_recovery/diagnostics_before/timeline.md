# Casa 2 Timeline

This timeline is assembled from the job status API, read-only database snapshot, request timing files, and runtime-cache metadata. Log files were not found in the inspected runtime-cache paths, so this is an evidence timeline rather than a full worker stdout transcript.

## Timeline

| Time (UTC) | Event | Evidence |
| --- | --- | --- |
| `2026-06-27T09:23:18.127Z` | Casa 2 temporal project created. | Project row and `project.json` |
| `2026-06-27T09:23:37Z` | Project metadata last persisted before run execution. | Project row, `project.json`, compact metadata |
| `2026-06-27T09:23:37.571789Z` | Temporal project job created. | Job row/API |
| `2026-06-27T09:52:29.218483Z` | Job started. | Job row/API |
| `2026-06-27T09:52:32.862245Z` | Request-level processing started for run `43a37e757f6111e5929acc40`. | `timing.json` |
| `2026-06-27T09:52:33Z` | Request manifest created. | `manifest.json` |
| `2026-06-27T10:11:38.900925Z` | Tiled inference stage started. | `timing.json` |
| `2026-06-27T13:43:22.784348Z` | Tiled inference stage completed successfully. | `timing.json` |
| `2026-06-27T13:43:33.672027Z` | Manifest write stage started. | `timing.json` |
| `2026-06-27T13:43:45.763427Z` | Manifest write stage completed successfully. | `timing.json` |
| `2026-06-27T13:43:45.763679Z` | Request-level processing completed. | `timing.json` |
| `2026-06-27T13:43:58.044273Z` | Job still reported `running`, stage `saving_artifacts`, progress `92`, message `Completed`. | Job row/API |

## Request Output Completion

- Request/run hash: `43a37e757f6111e5929acc40`
- Processed tiles: `6090`
- Total tiles: `6090`
- Feature count: `15528`
- Request `run_response.json` reports `success: true`
- Core outputs present:
  - `prediction_change_probability.tif`
  - `prediction_change_mask.tif`
  - `prediction_change_polygons.geojsonl`
  - `building_change_polygons.geojson`
  - `manifest.json`
  - `tiled_inference_metadata.json`
  - `timing.json`
  - `run_response.json`

## Project Finalization State

At the time of inspection, Casa 2 project state had not advanced after project creation:

- All three milestones were still `pending`.
- All milestone `metrics` fields were `None`.
- All milestone `pair_request_hash` and `populated_request_hash` fields were `None`.
- Project artifact count was `0`.
- Project run count was `0`.
- No project metadata file contained request hash `43a37e757f6111e5929acc40`.

## Live Worker Note

During inspection, the Celery task was visible as `STARTED`, and Redis showed the Casa 2 task in the unacked set rather than the queue. Celery inspect did not reply within the timeout, consistent with a busy solo worker. This means the state could still change after the snapshot if the worker eventually finishes the finalization phase.
