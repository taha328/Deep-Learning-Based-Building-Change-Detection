$ErrorActionPreference = "Stop"

$DeployDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $DeployDir

$output = docker compose --env-file .env run --rm -e MODEL_DEVICE=auto backend-api /app/backend/.venv/bin/python /app/backend/scripts/validate_bandon_runtime.py --json
Write-Host $output

$payload = $output | ConvertFrom-Json
if ($payload.device_resolved -ne "cpu") {
  throw "Expected MODEL_DEVICE=auto to resolve to cpu in the supported CPU deployment; got '$($payload.device_resolved)'."
}

Write-Host "Runtime validation passed."
