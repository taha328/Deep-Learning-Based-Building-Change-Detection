# Native Windows Setup Without Docker

Docker is the recommended runtime for normal installs. Native Windows mode is for development, debugging, and machines where Docker Desktop is not available. It installs and runs the API, Celery worker, frontend, PostgreSQL/PostGIS, Memurai, and the BANDON model directly on Windows.

## Requirements

- Windows 10 or Windows 11, 64-bit.
- PowerShell launched as Administrator. UAC elevation is required because setup installs packages and starts Windows services.
- Internet access for `winget`, npm, pip, GitHub Releases, and the PostGIS bundle.
- At least 16 GB RAM; 24 GB or more is recommended for CPU inference.
- Enough free disk space for Python packages, npm packages, PostgreSQL data, runtime cache, and the model artifact.

## One-Time Setup

Open PowerShell as Administrator, then run:

```powershell
git clone https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection.git
cd Deep-Learning-Based-Building-Change-Detection
powershell -ExecutionPolicy Bypass -File scripts\setup-windows-native.ps1
```

The setup script is idempotent and non-destructive:

- Existing `backend\.env` and `frontend\.env.local` are preserved by default.
- Pass `-ForceEnv` to back up and regenerate those env files from the Windows templates.
- Existing model artifacts are reused by default.
- Pass `-ForceModelDownload` to redownload the authorized `mtgcdnet_iter_40000.pth` artifact.
- Pass `-SkipFrontendBuild` to skip `npm run build`.
- Pass `-NoStart` to provision everything without launching the app.

Examples:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-windows-native.ps1 -NoStart
powershell -ExecutionPolicy Bypass -File scripts\setup-windows-native.ps1 -ForceEnv -ForceModelDownload
```

Logs are written to:

```text
logs\setup-windows-native.log
```

## What Setup Installs And Verifies

The script uses `winget` where possible and verifies each component after install:

- Git
- Python 3.11 through the `py -3.11` launcher
- Node.js LTS and npm
- PostgreSQL 16
- PostGIS for PostgreSQL 16
- Memurai Developer as a Redis-compatible local service
- `backend\.venv`
- `backend\requirements.txt`
- `frontend\package-lock.json` through `npm ci`
- BANDON model artifact at `models\bandon\mtgcdnet_iter_40000.pth`
- PostgreSQL role/database, PostGIS extension, and Alembic migrations
- Backend import of `src.api.main` and `src.jobs.celery_app`

If PostgreSQL requires credentials, setup prompts for the local `postgres` password. The app database defaults are:

```text
database: building_change_app
user: building_change_user
password: building_change_password
```

## Start

If setup was run without `-NoStart`, the app starts automatically. To start later:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-windows-native.ps1
```

The script opens separate PowerShell windows for:

- FastAPI backend: `http://127.0.0.1:8000`
- Celery worker using `--pool=solo`
- Vite frontend: `http://127.0.0.1:5173`

It records launch PIDs in:

```text
logs\native-processes.json
```

## Health Check

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\health-windows-native.ps1
```

The health script verifies the native toolchain, PostgreSQL port, app database connection, PostGIS, Memurai PING, env keys, virtualenv, model size, backend imports, migrations, and API/frontend endpoints if those endpoints are running.

## Stop

Stop only the app processes recorded by `start-windows-native.ps1`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop-windows-native.ps1
```

PostgreSQL and Memurai services are intentionally left running. Stop those services only when explicitly requested:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop-windows-native.ps1 -StopServices
```

## Environment Files

Templates:

- `backend\.env.windows.example`
- `frontend\.env.local.windows.example`

Generated runtime files:

- `backend\.env`
- `frontend\.env.local`

The backend template points BANDON to the native virtualenv:

```text
APP_BANDON_ENV_PREFIX=<repo>\backend\.venv
APP_BANDON_CHECKPOINT_PATH=<repo>\models\bandon\mtgcdnet_iter_40000.pth
```

## Troubleshooting

If setup fails, read `logs\setup-windows-native.log` first. The most common causes are:

- PowerShell was not launched as Administrator.
- `winget` is missing or disabled by policy.
- PostgreSQL 16 is installed but its service is stopped.
- The `postgres` password entered during setup is incorrect.
- PostGIS could not be installed silently; install it with PostgreSQL StackBuilder or the OSGeo PostGIS bundle, then rerun setup.
- Native GIS Python wheels are unavailable for the current Python/platform combination. Keep Python at 3.11 and rerun setup after installing the missing system dependency.
- Port `5432`, `6379`, `8000`, or `5173` is already in use.

Rerun the health check after fixing an issue:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\health-windows-native.ps1
```

For the packaged release path, use the Docker installer in the README instead of native Windows mode.
