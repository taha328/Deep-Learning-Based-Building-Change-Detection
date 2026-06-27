# Current Casa City Job Recovery Plan

## Observed Status

- Project: `temporal-casa-city-mqwsmj0h-ycnlh1`
- Job: `job-f3eeba1c30964ea88481b71bcbe2c458`
- Celery task: `ca42f965-e8f4-4eec-8a0c-be65fb124d04`
- Status at latest check: `running`
- Stage: `fetching_imagery`
- Progress: `33095/55704` processed
- Failures/retries: `failed_tile_count=0`, `retry_count=0`
- Worker PID: `67060`
- Redis queues checked: `building_change=0`, `celery=0`
- Free disk: about 80 GiB

## Partial File Inspection

No `*.partial`, `*.tmp`, `*.tmp.tif`, or lock files were found in the Wayback mosaic cache or current request/tmp workspaces during inspection.

## Safe Recovery Position

No recovery action was executed because the job is still active. The active Celery worker was started before this code change, so it is likely using the old imported code for the current run. If it later fails with the classic TIFF overflow, do not reuse any failed partial mosaic. Restart the Celery worker after this fix is deployed, then restart the temporal job or run a documented stage-level rebuild if one exists.

Current code inspection did not identify a safe, user-facing stage-level restart command for only the mosaic/reference imagery stage. If the active job fails, the safest supported recovery path is to restart the temporal job after the fixed worker is running. Existing preflight and tile caches can still reduce repeated remote work.
