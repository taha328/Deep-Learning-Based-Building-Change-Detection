# Casa 2 After-State Evidence

## Job API

Source: `api_job_after.json`

- `status`: `failed`
- `stage`: `failed`
- `progress`: `100`
- `completed_at`: `2026-06-27T14:27:07.518246Z`
- `error_code`: `temporal_partial_recovery`

The job is no longer stuck in `running/saving_artifacts`. It is terminal. It is not marked completed because only the first pair was recovered from the completed request and the second required pair was never run.

## Compact Project API

Source: `api_compact_project_after.json`

- `loading_mode`: `compact`
- `milestone_count`: `3`
- `complete_milestone_count`: `1`
- `WB_2024_R02`: `pending`, baseline metrics present
- `WB_2025_R03`: `complete`, metrics present, existing artifacts present
- `WB_2026_R05`: `pending`, no metrics

Recovered `WB_2025_R03` metrics:

- `added_area_m2`: `6152643.19`
- `additions_feature_count`: `15528`
- Existing compact artifacts: `additions`, `building_change_buffer_10m`, `building_change_buffer_15m`, `building_change_buffer_20m`

## UI Verification

Source: `ui_casa2_metrics_after.json` and `ui_casa2_metrics_after.png`

- Metrics unavailable warning visible: `false`
- Growth overview visible: `true`
- Added surface visible: `true`
- Detected additions visible: `true`
- Spatial composition visible: `true`
- `6.15 km2` visible: `true`
- `15528` additions visible: `true`

## Queue and Process State

- Redis `building_change` queue length: `0`
- Redis `unacked` count: `0`
- Redis `unacked_index` count: `0`
- Dev backend, frontend, and Celery processes were stopped after verification.
