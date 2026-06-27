# Casa 2 DB Snapshot (Read Only)

Captured from read-only SELECT queries against the configured application database. No INSERT, UPDATE, DELETE, Redis mutation, task revoke, or project/runtime-cache mutation was performed.

## Application Settings Seen By Backend

- `persistence_backend`: `filesystem`
- Database URL: PostgreSQL on `localhost:5432/building_change` (credentials redacted)

## Project Row

- `project_id`: `temporal-casa-2-mqw5jysv-ri6q84`
- `name`: `CASA 2`
- `created_at`: `2026-06-27T09:23:18.127000+00:00`
- `updated_at`: `2026-06-27T09:23:37+00:00`
- `project_dir`: `/Users/tahaelouali/Developer/Building_change_app/backend/runtime_cache/temporal_projects/temporal-casa-2-mqw5jysv-ri6q84`
- Raw payload keys: `path`, `schema`, `sha256`, `size_bytes`, `storage`

## Milestone Rows

| Milestone | Status | Pair request hash | Populated request hash | Metrics | Error |
| --- | --- | --- | --- | --- | --- |
| `WB_2024_R02` | `pending` | `None` | `None` | `None` | `None` |
| `WB_2025_R03` | `pending` | `None` | `None` | `None` | `None` |
| `WB_2026_R05` | `pending` | `None` | `None` | `None` | `None` |

The milestone raw payloads did not contain request hash `43a37e757f6111e5929acc40`.

## Artifact And Run Rows

- Temporal artifact rows for project: `0`
- Run rows associated with project: `0`
- `RunRecord` for run id `43a37e757f6111e5929acc40`: not found

## Job Row

- `job_id`: `job-aa092d18d777402994b8e8fe24b59893`
- `celery_task_id`: `27f5aeb4-fdf0-4151-84eb-5c656ecffd56`
- `job_kind`: `temporal_project`
- `status`: `running`
- `stage`: `saving_artifacts`
- `progress`: `92`
- `message`: `Completed`
- `project_id`: `temporal-casa-2-mqw5jysv-ri6q84`
- `result_run_id`: `None`
- `cancel_requested`: `False`
- `created_at`: `2026-06-27T09:23:37.571789Z`
- `started_at`: `2026-06-27T09:52:29.218483Z`
- `updated_at`: `2026-06-27T13:43:58.044273Z`
- `completed_at`: `None`
- Raw result keys: `progress_details`

## Interpretation

The database state does not show a completed temporal project publication. The request `43a37e757f6111e5929acc40` exists on disk and reports successful inference output, but no DB rows connect that request to Casa 2 milestones, project artifacts, project runs, or a completed job result.
