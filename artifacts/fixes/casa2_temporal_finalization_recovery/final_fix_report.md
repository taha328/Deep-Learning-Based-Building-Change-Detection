# Final Fix Report

## Outcome

Casa 2 milestone `WB_2025_R03` was recovered from completed request `43a37e757f6111e5929acc40` without rerunning the 6090-tile inference. The project compact API and frontend now show recovered spatial indicators for that milestone.

The original job `job-aa092d18d777402994b8e8fe24b59893` is terminal `failed` with `error_code=temporal_partial_recovery`. This is intentional: the original temporal job had two required pairs, but only `WB_2024_R02 -> WB_2025_R03` had completed request outputs. Marking the whole job complete would hide partial completion.

## Root Cause

The request-level tiled inference completed successfully, but temporal finalization then attempted expensive derived geometry generation for 15528 additions. The worker became CPU-bound inside GEOS buffer/noding work before durable project/job finalization completed.

A second persistence issue made the compact API stale: the repository save path rewrote `project.json` after the service wrote compact sidecars, causing compact metadata to be ignored after later loads.

## Code Fixes

- Added `temporal_derived_geometry_max_features` to cap expensive derived temporal geometry generation.
- Added summary-backed finalization for oversized completed request payloads.
- Skipped buffer and derived geometry regeneration when feature counts exceed the configured limit.
- Preserved metrics and request hash while avoiding heavyweight geometry recomputation.
- Made request publication idempotent when metrics and additions artifacts already exist.
- Added worker finalization guards so successful responses cannot be marked complete unless project metrics, request hash, and artifacts are durably published.
- Updated repository persistence to rewrite `project_summary.json` and `project_compact_metadata.json` after `project.json`.
- Fixed compact project completion counts to match returned milestone statuses.

## Files Changed

- `backend/src/config.py`
- `backend/src/jobs/tasks.py`
- `backend/src/repositories/temporal_project_repository.py`
- `backend/src/services/temporal_projects.py`
- `backend/tests/test_jobs.py`
- `backend/tests/test_temporal_project_compact_loading.py`
- `backend/tests/test_temporal_projects.py`

## Evidence

- Before state: `before_state.md`, `api_job_before.json`, `api_compact_project_before.json`, `db_before_snapshot.json`
- Worker and Redis resolution: `worker_state_resolution.md`, `worker_liveness_30s.txt`, `worker_70726_sample.txt`, `redis_orphan_cleanup.txt`
- Recovery execution: `casa2_recovery_execution.md`, `casa2_recovery_execution.json`
- After state: `after_state_evidence.md`, `api_job_after.json`, `api_compact_project_after.json`, `db_after_snapshot.json`
- UI evidence: `ui_casa2_metrics_after.json`, `ui_casa2_metrics_after.png`
- Tests: `test_results.md`, `backend_pytest_full.log`, `frontend_test.log`, `frontend_build.log`
