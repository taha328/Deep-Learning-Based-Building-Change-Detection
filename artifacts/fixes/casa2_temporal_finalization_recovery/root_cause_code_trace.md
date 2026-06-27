# Root Cause Code Trace

## Request Execution

`backend/src/core_api.py` calls `run_temporal_project(...)` from `run_temporal_project_api(...)`. Each temporal pair is executed through a pair runner that calls `run_detection(...)`.

For Casa 2, request `43a37e757f6111e5929acc40` completed successfully and wrote `run_response.json`, rasters, and vectors. The cached response contains:

- `change_polygons_geojson`: 15,528 features
- `buffer_layers_geojson`: empty
- `building_blocks_geojson`: null

## Project Finalization

In `backend/src/services/temporal_projects.py`, `run_temporal_project(...)` applies a successful pair response with `_apply_pair_response_to_milestone(...)`, then recomputes project outputs with `_recompute_project_outputs_from_index(...)`, then calls `_refresh_project_bundle(...)` and `_save_project(...)`.

The request hash should be written by `_apply_pair_response_to_milestone(...)`:

- `milestone.pair_request_hash`
- `milestone.populated_request_hash`
- `milestone.request_workspace_path`

The final project state should be saved by `_save_project(...)` and, in Postgres mode, by `save_project_record(...)` in `run_temporal_project_api(...)`.

## Failure Point

The worker sample shows the task stuck in GEOS buffer generation. The relevant service path is `_hydrate_milestone_buffer_layers(...)`.

When a milestone has additions but no buffer layers from the cached response or from existing artifact files, `_hydrate_milestone_buffer_layers(...)` calls `build_change_buffer_layers(...)` for 10m, 15m, and 20m buffers.

For Casa 2 this means buffering 15,528 polygons during post-inference temporal finalization. That work happened after request-level success and before project/job persistence, so:

- the request output existed and was valid
- the project still had no saved milestone metrics or artifacts
- the job remained running at `saving_artifacts`

## Why Export Bundle Skipping Is Not The Root Cause

`TILED_INFERENCE_EXPORT_BUNDLE_SKIPPED reason=disabled_by_default` affects the request export zip. It does not explain missing milestone metrics by itself. The real issue is that temporal finalization tried to regenerate heavy spatial derivatives after request success and did not persist the core completed milestone before doing that work.

## Existing Recovery Path

`publish_completed_tiled_request(...)` already implements the correct recovery direction: load a completed request response, attach it to a temporal milestone, recompute outputs, refresh artifacts/metadata, and save the project. It was not sufficient for Casa 2 before this fix because it reused the same unbounded buffer-generation path.

## Required Fix Shape

The fix must:

- persist core completed milestone outputs from the completed request without rerunning inference
- make missing buffer regeneration bounded/optional for large feature sets
- keep recovery idempotent
- validate that completed temporal jobs have persisted milestone metrics/artifacts before marking the job completed
- fail explicitly if finalization cannot persist usable project state
