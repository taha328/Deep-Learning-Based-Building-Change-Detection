# Docker CPU Pipeline Validation

Validated against the raw-installed `v0.1.4` Docker bundle.

Install directory:

```text
/Users/tahaelouali/.local/share/building-change-app/releases/20260629T100908Z.l67G6H/building-change-app
```

Running service state before cleanup:

```text
building-change-frontend-1        ghcr.io/taha328/building-change-frontend:v0.1.4      Up 2 minutes (healthy)   127.0.0.1:8080->80/tcp
building-change-backend-api-1     ghcr.io/taha328/building-change-backend:cpu-v0.1.4   Up 2 minutes (healthy)   127.0.0.1:8000->8000/tcp
building-change-celery-worker-1   ghcr.io/taha328/building-change-backend:cpu-v0.1.4   Up 2 minutes             8000/tcp
building-change-redis-1           redis:7-alpine                                       Up 2 minutes (healthy)   6379/tcp
building-change-postgres-1        imresamu/postgis:16-3.4                              Up 2 minutes (healthy)   5432/tcp
```

Runtime validation:

- Command: `./scripts/validate-runtime.sh`
- Result: passed
- `device_requested`: `auto`
- `device_resolved`: `cpu`
- `torch_version`: `2.12.1+cpu`
- `mmcv_version`: `1.7.0`
- Checkpoint path: `/models/bandon/mtgcdnet_iter_40000.pth`

Smoke test:

- Command: `./scripts/smoke-test.sh`
- Result: passed
- Job id: `job-81c4fbf24f3e4050a034a34f94c584eb`
- Request hash: `25d8e23d09315e8fd2cdc44b`
- Device resolved: `cpu`
- Artifact count: `14`
- Retrieved PNG: `/data/runtime_cache/requests/25d8e23d09315e8fd2cdc44b/t1_preview.png`

Cleanup:

- Command: `./scripts/stop.sh`
- Result: passed
- Final `docker ps`: no running containers.
