$ErrorActionPreference = "Stop"

$DeployDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$CheckpointName = "mtgcdnet_iter_40000.pth"
$FinalDir = Join-Path $DeployDir "models\bandon"
$FinalPath = Join-Path $FinalDir $CheckpointName

if ($env:MODEL_ARTIFACT_FILE -and $env:MODEL_ARTIFACT_URL) {
  throw "Set only one of MODEL_ARTIFACT_FILE or MODEL_ARTIFACT_URL."
}

if (-not $env:MODEL_ARTIFACT_FILE -and -not $env:MODEL_ARTIFACT_URL) {
  throw @"
No model artifact source was provided.

Install from a local artifact:
  `$env:MODEL_ARTIFACT_FILE="C:\path\building-change-model-bandon-mtgcdnet-v0.1.0.zip"
  .\scripts\windows\fetch-model.ps1

Or set MODEL_ARTIFACT_URL to a controlled download URL.
"@
}

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("building-change-fetch-model-" + [System.Guid]::NewGuid())
$ArtifactZip = Join-Path $TempDir "model-artifact.zip"
$ExtractDir = Join-Path $TempDir "extracted"

try {
  New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null

  if ($env:MODEL_ARTIFACT_FILE) {
    if (-not (Test-Path -LiteralPath $env:MODEL_ARTIFACT_FILE -PathType Leaf)) {
      throw "Model artifact file not found: $($env:MODEL_ARTIFACT_FILE)"
    }
    Copy-Item -LiteralPath $env:MODEL_ARTIFACT_FILE -Destination $ArtifactZip
  } else {
    $Headers = @{ Accept = "application/octet-stream" }
    if ($env:MODEL_ARTIFACT_AUTH_HEADER) {
      $HeaderParts = $env:MODEL_ARTIFACT_AUTH_HEADER -split ":", 2
      if ($HeaderParts.Count -ne 2) {
        throw "MODEL_ARTIFACT_AUTH_HEADER must use the format 'Header-Name: value'."
      }
      $Headers[$HeaderParts[0].Trim()] = $HeaderParts[1].Trim()
    }
    Invoke-WebRequest -Uri $env:MODEL_ARTIFACT_URL -Headers $Headers -OutFile $ArtifactZip
  }

  Expand-Archive -LiteralPath $ArtifactZip -DestinationPath $ExtractDir
  $Checkpoint = Get-ChildItem -Path $ExtractDir -Recurse -File -Filter $CheckpointName |
    Where-Object { $_.FullName -match "[\\/]models[\\/]bandon[\\/]mtgcdnet_iter_40000\.pth$" } |
    Select-Object -First 1
  if (-not $Checkpoint) {
    throw "Artifact does not contain models\bandon\$CheckpointName."
  }

  $ChecksumFile = Get-ChildItem -Path $ExtractDir -Recurse -File -Filter "SHA256SUMS.txt" | Select-Object -First 1
  if ($ChecksumFile) {
    $ChecksumLine = Get-Content -LiteralPath $ChecksumFile.FullName |
      Where-Object { $_ -match "models/bandon/mtgcdnet_iter_40000\.pth$" } |
      Select-Object -First 1
    if (-not $ChecksumLine) {
      throw "SHA256SUMS.txt does not contain models/bandon/$CheckpointName."
    }
    $ExpectedHash = ($ChecksumLine -split "\s+", 2)[0].ToUpperInvariant()
    $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Checkpoint.FullName).Hash.ToUpperInvariant()
    if ($ActualHash -ne $ExpectedHash) {
      throw "Checkpoint SHA256 verification failed."
    }
    Write-Host "Checkpoint SHA256 verification passed."
  } else {
    Write-Warning "Artifact does not contain SHA256SUMS.txt; checkpoint checksum was not verified."
  }

  New-Item -ItemType Directory -Force -Path $FinalDir | Out-Null
  Copy-Item -LiteralPath $Checkpoint.FullName -Destination $FinalPath -Force
  $Installed = Get-Item -LiteralPath $FinalPath
  Write-Host "Model checkpoint installed."
  Write-Host "Path: $($Installed.FullName)"
  Write-Host "Size: $($Installed.Length) bytes"
} finally {
  Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
}
