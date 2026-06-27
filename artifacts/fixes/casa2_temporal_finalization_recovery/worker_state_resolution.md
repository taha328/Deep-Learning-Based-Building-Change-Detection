# Worker State Resolution

## Initial Worker State

Before recovery, the dev stack had a live Celery worker:

- PID: `70726`
- Command: `python -m celery -A src.jobs.celery_app.celery_app worker --loglevel=INFO --queues building_change --pool solo --hostname building_change_worker@%h`
- Redis queue `building_change`: empty
- Redis `unacked`: one delivery tag, `2749d830-4f59-4d01-93c3-a79e8b268c00`
- Celery task meta for `27f5aeb4-fdf0-4151-84eb-5c656ecffd56`: `STARTED`
- Job state remained `running`, stage `saving_artifacts`, progress `92`

## Bounded Liveness Check

The worker was observed over a 30-second interval:

- CPU remained around 97-100%.
- Job `updated_at` remained `2026-06-27T13:43:58.044273Z`.
- Job stage remained `saving_artifacts`.
- Job completion fields remained unset.

## Process Sample

`sample` output in `worker_70726_sample.txt` showed the worker burning CPU inside GEOS buffer operations after inference had completed. The dominant stack was in `GEOSBufferWithParams_r` and related GEOS buffer/noding calls.

This matches the code path where temporal finalization tries to regenerate missing buffer layers from the large additions GeoJSON.

## Resolution

The dev stack was stopped with `scripts/dev_stop_all.sh`.

- Backend: stopped
- Frontend: stopped
- Celery: required SIGKILL
- Redis: left running

After stopping, no Celery worker remained. The orphaned Redis delivery tag was then verified to contain:

- Job id `job-aa092d18d777402994b8e8fe24b59893`
- Celery task id `27f5aeb4-fdf0-4151-84eb-5c656ecffd56`
- Project id `temporal-casa-2-mqw5jysv-ri6q84`

Only that delivery tag was removed:

- `HDEL unacked 2749d830-4f59-4d01-93c3-a79e8b268c00`: `1`
- `ZREM unacked_index 2749d830-4f59-4d01-93c3-a79e8b268c00`: `1`

No Redis database flush was performed.
