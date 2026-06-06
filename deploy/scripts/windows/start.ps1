$ErrorActionPreference = "Stop"

$DeployDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $DeployDir

$Checkpoint = Join-Path $DeployDir "models\bandon\mtgcdnet_iter_40000.pth"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker is required. Install Docker Desktop first."
}

docker compose version | Out-Null

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example."
}

if (-not (Test-Path $Checkpoint)) {
  throw @"
Missing model checkpoint: $Checkpoint

Install it with:
  `$env:MODEL_ARTIFACT_FILE="C:\path\building-change-model-bandon-mtgcdnet-v0.1.0.zip"
  .\scripts\windows\fetch-model.ps1

or download the model artifact and place:
  models\bandon\mtgcdnet_iter_40000.pth
"@
}

Write-Host "Pulling images if available..."
docker compose --env-file .env pull
if ($LASTEXITCODE -ne 0) {
  Write-Host "Image pull did not complete; continuing with locally available images."
}

Write-Host "Starting database and Redis..."
docker compose --env-file .env up -d postgres redis

Write-Host "Waiting for postgres and redis..."
Start-Sleep -Seconds 10

Write-Host "Verifying PostgreSQL accepts application connections..."
$connected = $false
for ($i = 0; $i -lt 60; $i++) {
  $check = @'
import os
import psycopg
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
with psycopg.connect(
    host=url.host,
    port=url.port,
    user=url.username,
    password=url.password,
    dbname=url.database,
    connect_timeout=5,
):
    pass
'@
  $check | docker compose --env-file .env run --rm --no-deps backend-api /app/backend/.venv/bin/python -
  if ($LASTEXITCODE -eq 0) {
    $connected = $true
    break
  }
  Start-Sleep -Seconds 2
}
if (-not $connected) {
  throw "PostgreSQL did not accept application connections before timeout."
}

Write-Host "Running PostgreSQL/PostGIS migration and verification..."
docker compose --env-file .env run --rm backend-api /app/backend/.venv/bin/python /app/backend/scripts/setup_postgis_db.py --migrate --verify

Write-Host "Starting application services..."
docker compose --env-file .env up -d backend-api celery-worker frontend

$envValues = Get-Content ".env" | Where-Object { $_ -match "^[A-Za-z_][A-Za-z0-9_]*=" } | ForEach-Object {
  $parts = $_ -split "=", 2
  @{ Key = $parts[0]; Value = $parts[1] }
}
$FrontendPort = ($envValues | Where-Object { $_.Key -eq "FRONTEND_PORT" } | Select-Object -Last 1).Value
$BackendPort = ($envValues | Where-Object { $_.Key -eq "BACKEND_PORT" } | Select-Object -Last 1).Value
if (-not $FrontendPort) { $FrontendPort = "8080" }
if (-not $BackendPort) { $BackendPort = "8000" }

Write-Host ""
Write-Host "Building Change Detection is starting."
Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
Write-Host "Backend diagnostics: http://127.0.0.1:$BackendPort/api/health"
Write-Host ""
Write-Host "Next checks:"
Write-Host "  .\scripts\windows\health.ps1"
Write-Host "  .\scripts\windows\validate-runtime.ps1"
Write-Host "  .\scripts\windows\smoke-test.ps1"
