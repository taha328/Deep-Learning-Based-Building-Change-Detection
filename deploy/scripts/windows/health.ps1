$ErrorActionPreference = "Stop"

$DeployDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $DeployDir

$FrontendPort = "8080"
if (Test-Path ".env") {
  $line = Get-Content ".env" | Where-Object { $_ -match "^FRONTEND_PORT=" } | Select-Object -Last 1
  if ($line) { $FrontendPort = ($line -split "=", 2)[1] }
}

$BaseUrl = "http://127.0.0.1:$FrontendPort"

function Test-Url($Label, $Url) {
  Write-Host "Checking $Label`: $Url"
  Invoke-RestMethod -Uri $Url -TimeoutSec 30 | Out-Null
}

Test-Url "frontend" "$BaseUrl/"
Test-Url "backend health" "$BaseUrl/api/health"
Test-Url "database health" "$BaseUrl/api/health/db"
Test-Url "redis health" "$BaseUrl/api/health/redis"
Test-Url "backend registry" "$BaseUrl/api/backends"

Write-Host "Health checks passed."
