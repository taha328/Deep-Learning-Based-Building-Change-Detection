# Raw Installer Validation

Validated with the exact raw installer command:

```bash
curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash
```

Result: passed.

Install directory:

```text
/Users/tahaelouali/.local/share/building-change-app/releases/20260629T100908Z.l67G6H/building-change-app
```

Observed installer lifecycle:

- Downloaded latest `building-change-app.zip` release asset.
- Pulled:
  - `ghcr.io/taha328/building-change-backend:cpu-v0.1.4`
  - `ghcr.io/taha328/building-change-frontend:v0.1.4`
  - `imresamu/postgis:16-3.4`
  - `redis:7-alpine`
- Started Postgres and Redis.
- Verified PostgreSQL accepted application connections.
- Applied and verified PostgreSQL/PostGIS migrations.
- Started backend API, Celery worker, and frontend.

Installed bundle health check:

```text
Checking frontend: http://127.0.0.1:8080/
Checking backend health: http://127.0.0.1:8080/api/health
Checking database health: http://127.0.0.1:8080/api/health/db
Checking redis health: http://127.0.0.1:8080/api/health/redis
Checking backend registry: http://127.0.0.1:8080/api/backends
Health checks passed.
```
