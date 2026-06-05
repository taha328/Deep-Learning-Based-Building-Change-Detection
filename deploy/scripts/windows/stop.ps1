$ErrorActionPreference = "Stop"

$DeployDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $DeployDir

docker compose --env-file .env down
