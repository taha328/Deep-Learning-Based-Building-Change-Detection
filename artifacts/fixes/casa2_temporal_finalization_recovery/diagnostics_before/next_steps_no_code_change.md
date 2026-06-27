# Next Steps (No Code Change)

These steps are operational recommendations only. They were not executed during this inspection.

## Immediate Read-Only Checks

1. Re-check job status after the live worker has had time to finish:
   - `GET /api/jobs/job-aa092d18d777402994b8e8fe24b59893`
   - `GET /api/temporal-projects/temporal-casa-2-mqw5jysv-ri6q84/compact`

2. Verify whether the project state changes from:
   - milestones `pending` -> `complete`
   - milestone `metrics: null` -> populated metrics object
   - milestone `pair_request_hash: null` -> `43a37e757f6111e5929acc40`
   - job `running` -> `completed`

3. If the job remains in `saving_artifacts`, inspect worker stdout or the terminal running `scripts/dev_start_all.py` for exceptions after request completion around `2026-06-27T13:43:45Z`.

## Non-Destructive Diagnosis To Prioritize

1. Determine whether the active Celery worker is still doing finalization work or is stuck in a post-inference step.
2. Look for any exception after request `43a37e757f6111e5929acc40` completed and before project save/job completion.
3. Inspect temporal finalization logs for functions in this path:
   - `_apply_pair_response_to_milestone`
   - `_recompute_project_outputs_from_index`
   - `_refresh_project_bundle`
   - `_save_project`
   - job completion persistence in `backend/src/jobs/tasks.py`

## Recovery Options To Consider Later

Do not run these until the live worker state is understood.

1. If the worker eventually completes:
   - No recovery may be needed; refresh the UI and verify metrics appear.

2. If the worker is stuck but still owns the task:
   - Avoid starting duplicate publication until the task is stopped or proven dead.
   - Capture worker traceback or logs first.

3. If the task is confirmed dead/stuck and request output is valid:
   - Use the existing publication/finalization path to publish completed request `43a37e757f6111e5929acc40` into project `temporal-casa-2-mqw5jysv-ri6q84`.
   - The codebase contains `publish_completed_tiled_request`, which appears designed for this type of request-to-temporal-project publication.
   - Treat this as an operational recovery action, not a code fix, and run it only after ensuring the original task will not concurrently mutate the same project.

4. After any recovery:
   - Re-query the compact project endpoint.
   - Confirm at least one milestone has `status: complete`, non-null `metrics`, and artifact entries.
   - Confirm the UI no longer shows the unavailable-indicators empty state for completed milestones.

## What Not To Do Blindly

- Do not delete request output directory `backend/runtime_cache/requests/43a37e757f6111e5929acc40`.
- Do not clear Redis unacked state while the worker may still be processing.
- Do not manually edit `project.json` or compact metadata by hand.
- Do not restart or duplicate the same temporal job until the active task state is resolved.
