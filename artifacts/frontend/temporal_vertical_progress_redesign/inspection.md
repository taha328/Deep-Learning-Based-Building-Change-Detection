# Temporal Vertical Progress UI Inspection

Date: 2026-06-27

## Current Renderer

- Temporal project progress is rendered from `frontend/src/features/temporal/TemporalMosaicPanel.tsx`.
- The panel uses `RunProgressPanel` from `frontend/src/features/results/RunProgressPanel.tsx`.
- Before this change, temporal jobs with `progress_details` rendered two horizontal bars:
  - global pair/model analysis progress
  - current pair/model analysis progress
  - one compact backend stage label

## Available Job Fields

The frontend job contract exposes:

- `status`: `queued`, `running`, `completed`, `failed`, `cancel_requested`, `cancelled`
- `progress`: integer percent
- `stage`: backend stage string such as `preflight`, `fetching_imagery`, `inference`, `vectorizing`, `building_buffers`, `saving_artifacts`, `persisting`
- `message`: backend status text
- `error_code` and `error_message`
- `result_run_id`
- `progress_details`

Temporal `progress_details` are parsed into:

- `current_pair_index`
- `total_pair_count`
- `pair_fraction`
- `pair_stage`
- `from_release_identifier`
- `to_release_identifier`
- `from_release_date`
- `to_release_date`

## Backend Finalization Values

`backend/src/jobs/tasks.py` emits temporal job progress around finalization:

- `saving_artifacts` at roughly 90%, message: `Saving temporal outputs and generated artifacts.`
- `persisting` at roughly 95%, message: `Persisting compact job metadata.`
- terminal completion is written by `mark_job_completed`

The callback inside the temporal run can also map late pair fractions to `saving_artifacts`. This creates the confusing case where `pair_fraction=1.0` while the job is still `running`.

## Existing Vertical Component

No dedicated temporal vertical progress component remained. The generic pipeline renderer in `RunProgressPanel` had vertical rows for non-temporal jobs, while temporal jobs took the two-horizontal-bar branch.

## Frontend-Only Feasibility

The existing job API has enough fields to fix the misleading UI:

- Pair/model progress comes from `progress_details.pair_fraction`.
- Project-level finalization comes from `stage`, `message`, and non-terminal `status`.
- Final readiness comes from terminal `status=completed` and the frontend's existing successful job resolution path.
- Failure and cancellation can be shown from `status`, `rawEvent`, and error detail.

No backend execution behavior change is required for this UI fix.

## Files Inspected

- `frontend/src/features/results/RunProgressPanel.tsx`
- `frontend/src/lib/run-progress.ts`
- `frontend/src/lib/run-progress.test.ts`
- `frontend/src/api/fastapi.ts`
- `frontend/src/api/contracts.ts`
- `frontend/src/features/temporal/TemporalMosaicPanel.tsx`
- `backend/src/jobs/tasks.py`
- `backend/src/core_api.py`
- `backend/src/jobs/schemas.py`
- `backend/src/api/routes/jobs.py`
