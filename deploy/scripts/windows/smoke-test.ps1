$ErrorActionPreference = "Stop"

$DeployDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $DeployDir

$BackendPort = "8000"
if (Test-Path ".env") {
  $line = Get-Content ".env" | Where-Object { $_ -match "^BACKEND_PORT=" } | Select-Object -Last 1
  if ($line) { $BackendPort = ($line -split "=", 2)[1] }
}
$BaseUrl = "http://127.0.0.1:$BackendPort"

$Payload = @{
  aoi_geojson = @{
    type = "Polygon"
    coordinates = @(
      @(
        @(-7.0, 33.0),
        @(-6.9975, 33.0),
        @(-6.9975, 33.0025),
        @(-7.0, 33.0025),
        @(-7.0, 33.0)
      )
    )
  }
  t1_release = "WB_2026_R04"
  t2_release = "WB_2026_R05"
  mode = "fast_preview"
  inference_backend = "bandon_mps"
}

$Json = $Payload | ConvertTo-Json -Depth 20

Write-Host "Validating smoke AOI..."
$Validation = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/detection/validate" -Body $Json -ContentType "application/json" -TimeoutSec 120
if (-not $Validation.valid) {
  $Validation | ConvertTo-Json -Depth 20
  throw "Smoke AOI validation failed."
}

Write-Host "Submitting async detection job..."
$JobStart = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/jobs/detection" -Body $Json -ContentType "application/json" -TimeoutSec 120
$JobId = $JobStart.job_id
Write-Host "job_id=$JobId"

$Deadline = (Get-Date).AddMinutes(15)
do {
  Start-Sleep -Seconds 5
  $Job = Invoke-RestMethod -Uri "$BaseUrl/api/jobs/$JobId" -TimeoutSec 60
  Write-Host "status=$($Job.status) progress=$($Job.progress) stage=$($Job.stage)"
} while ((Get-Date) -lt $Deadline -and $Job.status -notin @("completed", "failed", "cancelled"))

if ($Job.status -ne "completed") {
  $Job | ConvertTo-Json -Depth 20
  throw "Smoke job did not complete successfully."
}

$RequestHash = $Job.request_hash
if (-not $RequestHash) { $RequestHash = $Job.result_run_id }
if (-not $RequestHash -and $Job.raw_result) { $RequestHash = $Job.raw_result.request_hash }
if (-not $RequestHash) { throw "Smoke job completed without request hash." }

$Result = Invoke-RestMethod -Uri "$BaseUrl/api/cache/runs/$RequestHash" -TimeoutSec 120
if ($Result.success -ne $true) {
  $Result | ConvertTo-Json -Depth 20
  throw "Cached smoke result was not successful."
}

$DeviceResolved = $Result.diagnostics.backend.bandon.device_resolved
if ($DeviceResolved -ne "cpu") {
  $Result.diagnostics.backend.bandon | ConvertTo-Json -Depth 20
  throw "Expected device_resolved=cpu, got '$DeviceResolved'."
}

if (-not $Result.artifacts -or $Result.artifacts.Count -lt 1) {
  throw "Smoke result did not include artifacts."
}

$PngPath = $null
foreach ($artifact in $Result.artifacts) {
  if ($artifact.path -and ($artifact.path.EndsWith(".png") -or $artifact.media_type -eq "image/png")) {
    $PngPath = $artifact.path
    break
  }
}
if (-not $PngPath -and $Result.preview_images) {
  foreach ($property in $Result.preview_images.PSObject.Properties) {
    if ($property.Value -is [string] -and $property.Value.EndsWith(".png")) {
      $PngPath = $property.Value
      break
    }
  }
}
if (-not $PngPath) { throw "No PNG artifact or preview path was available for /api/files retrieval." }

$EncodedPath = [System.Uri]::EscapeDataString($PngPath)
$Bytes = Invoke-WebRequest -Uri "$BaseUrl/api/files?path=$EncodedPath" -TimeoutSec 120
if ($Bytes.RawContentLength -le 0) { throw "Retrieved PNG artifact was empty." }

Write-Host "Smoke test passed."
Write-Host "request_hash=$RequestHash"
Write-Host "device_resolved=$DeviceResolved"
Write-Host "artifact_count=$($Result.artifacts.Count)"
Write-Host "retrieved_png=$PngPath"
