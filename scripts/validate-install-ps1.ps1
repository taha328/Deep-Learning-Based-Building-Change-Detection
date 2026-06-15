$ErrorActionPreference = "Stop"
$env:BUILDING_CHANGE_INSTALLER_TEST_MODE = "1"
. (Join-Path $PSScriptRoot "..\install.ps1")

$script:Passed = 0

function Assert-Equal {
  param($Actual, $Expected, [string] $Message)
  if ($Actual -ne $Expected) {
    throw "$Message. Expected '$Expected', got '$Actual'."
  }
  $script:Passed++
}

function Assert-Throws {
  param([scriptblock] $Action, [string] $Pattern, [string] $Message)
  try {
    & $Action
  } catch {
    if ($_.Exception.Message -notmatch $Pattern) {
      throw "$Message. Unexpected error: $($_.Exception.Message)"
    }
    $script:Passed++
    return
  }
  throw "$Message. Expected an error."
}

$metadata = @'
{
  "tag_name": "v9.9.9",
  "assets": [
    {"name": "notes.txt", "browser_download_url": "https://example.invalid/notes.txt"},
    {"name": "building-change-app.zip", "browser_download_url": "https://example.invalid/app.zip"}
  ]
}
'@ | ConvertFrom-Json

$asset = Get-ReleaseAsset -Release $metadata -Name "building-change-app.zip"
Assert-Equal $asset.browser_download_url "https://example.invalid/app.zip" "Asset selection failed"
Assert-Throws { Get-ReleaseAsset -Release $metadata -Name "missing.zip" } "does not contain" "Missing asset protection failed"

Assert-Equal (Get-ReleaseApiUrl -RequestedVersion "latest") "https://api.github.com/repos/taha328/Deep-Learning-Based-Building-Change-Detection/releases/latest" "Latest release URL failed"
Assert-Equal (Get-ReleaseApiUrl -RequestedVersion "v1.2.3") "https://api.github.com/repos/taha328/Deep-Learning-Based-Building-Change-Detection/releases/tags/v1.2.3" "Tagged release URL failed"

$originalLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = Join-Path ([IO.Path]::GetTempPath()) "building-change-local-app-data"
try {
  $defaultPath = Get-DefaultInstallDirectory -Now ([datetime]"2026-06-15T12:34:56Z")
  Assert-Equal $defaultPath (Join-Path $env:LOCALAPPDATA "BuildingChangeDetection\releases\20260615T123456Z") "Default LOCALAPPDATA path failed"
} finally {
  $env:LOCALAPPDATA = $originalLocalAppData
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("building-change-installer-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
  Assert-Throws { Resolve-InstallDirectory -RequestedPath $tempRoot } "already exists" "Overwrite protection failed"
  Assert-Equal (Resolve-InstallDirectory -RequestedPath $tempRoot -AllowExisting) ([IO.Path]::GetFullPath($tempRoot)) "Force path resolution failed"

  $dryRunPath = Join-Path $tempRoot "dry-run-target"
  $plan = Invoke-BuildingChangeInstaller -RequestedInstallDir $dryRunPath -PlanOnly
  Assert-Equal $plan.Downloaded $false "DryRun attempted a download"
  Assert-Equal $plan.Extracted $false "DryRun attempted extraction"
  Assert-Equal $plan.DockerStarted $false "DryRun attempted to start Docker"
  Assert-Equal (Test-Path -LiteralPath $dryRunPath) $false "DryRun created the install directory"
} finally {
  Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$global:FakeDockerMode = "ready"
function global:docker {
  if ($global:FakeDockerMode -eq "stopped" -and $args[0] -eq "info") {
    $global:LASTEXITCODE = 1
    return
  }
  if ($global:FakeDockerMode -eq "compose-missing" -and $args[0] -eq "compose") {
    $global:LASTEXITCODE = 1
    return
  }
  $global:LASTEXITCODE = 0
}
try {
  Test-DockerPrerequisites
  $script:Passed++
  $global:FakeDockerMode = "stopped"
  Assert-Throws { Test-DockerPrerequisites } "not running" "Stopped Docker protection failed"
  $global:FakeDockerMode = "compose-missing"
  Assert-Throws { Test-DockerPrerequisites } "Docker Compose is unavailable" "Missing Docker Compose protection failed"
  Assert-Throws { Test-DockerPrerequisites -DockerCommand "definitely-missing-docker-command" } "Docker is not installed" "Missing Docker protection failed"
} finally {
  Remove-Item function:\global:docker -ErrorAction SilentlyContinue
  Remove-Variable FakeDockerMode -Scope Global -ErrorAction SilentlyContinue
  $global:LASTEXITCODE = 0
}

Write-Host "install.ps1 validation passed: $script:Passed assertions."
