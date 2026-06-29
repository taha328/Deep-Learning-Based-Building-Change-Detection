# Cleanup Evidence

Date: 2026-06-29

Command:

```sh
./scripts/stop.sh
```

Run from:

`/Users/tahaelouali/.local/share/building-change-app/releases/20260629T130818Z.22gBGj/building-change-app`

Result:

- stopped and removed `building-change-frontend-1`
- stopped and removed `building-change-backend-api-1`
- stopped and removed `building-change-celery-worker-1`
- stopped and removed `building-change-postgres-1`
- stopped and removed `building-change-redis-1`
- removed Docker network `building-change_default`

Final `docker ps`:

```text
NAMES     IMAGE     STATUS
```

No project containers remained running.
