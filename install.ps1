param(
  [string] $InstallDir,
  [string] $Version = "latest",
  [switch] $NoStart,
  [switch] $SkipHealth,
  [switch] $Force,
  [switch] $DryRun,
  [string] $AssetName = "building-change-app.zip"
)

$ErrorActionPreference = "Stop"

$script:Repository = "taha328/Deep-Learning-Based-Building-Change-Detection"
$script:AppUrl = "http://127.0.0.1:8080"
$script:ApiDocsUrl = "http://127.0.0.1:8000/docs"

function Get-ReleaseApiUrl {
  param([string] $RequestedVersion)

  if ([string]::IsNullOrWhiteSpace($RequestedVersion) -or $RequestedVersion -eq "latest") {
    return "https://api.github.com/repos/$script:Repository/releases/latest"
  }

  $encodedTag = [Uri]::EscapeDataString($RequestedVersion)
  return "https://api.github.com/repos/$script:Repository/releases/tags/$encodedTag"
}

function Get-DefaultInstallDirectory {
  param([datetime] $Now = [datetime]::UtcNow)

  $installBase = $env:LOCALAPPDATA
  if ([string]::IsNullOrWhiteSpace($installBase) -and -not [string]::IsNullOrWhiteSpace($env:HOME)) {
    $installBase = Join-Path $env:HOME ".local\share"
  }
  if ([string]::IsNullOrWhiteSpace($installBase)) {
    throw "No default application-data directory is available. Pass -InstallDir with a writable installation directory."
  }

  $timestamp = $Now.ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  return Join-Path $installBase "BuildingChangeDetection\releases\$timestamp"
}

function Resolve-InstallDirectory {
  param(
    [string] $RequestedPath,
    [switch] $AllowExisting
  )

  $path = $RequestedPath
  if ([string]::IsNullOrWhiteSpace($path)) {
    $path = Get-DefaultInstallDirectory
  }
  $path = [IO.Path]::GetFullPath($path)

  if ((Test-Path -LiteralPath $path) -and -not $AllowExisting) {
    throw "Installation directory already exists: $path. Choose another -InstallDir or rerun with -Force."
  }

  return $path
}

function Get-ReleaseAsset {
  param(
    [Parameter(Mandatory = $true)] $Release,
    [Parameter(Mandatory = $true)][string] $Name
  )

  $asset = @($Release.assets | Where-Object { $_.name -eq $Name } | Select-Object -First 1)
  if ($asset.Count -eq 0 -or [string]::IsNullOrWhiteSpace($asset[0].browser_download_url)) {
    $tag = if ($Release.tag_name) { $Release.tag_name } else { "requested release" }
    throw "Release '$tag' does not contain the required asset '$Name'."
  }

  return $asset[0]
}

function Test-DockerPrerequisites {
  param([string] $DockerCommand = "docker")

  if (-not (Get-Command $DockerCommand -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed. Install Docker Desktop for Windows, then rerun this installer."
  }

  & $DockerCommand info *> $null
  if ($LASTEXITCODE -ne 0) {
    throw "Docker is installed but is not running. Start Docker Desktop, wait until it is ready, then rerun this installer."
  }

  & $DockerCommand compose version *> $null
  if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose is unavailable. Update Docker Desktop to a version that provides the 'docker compose' command."
  }
}

function Get-EnvironmentValue {
  param(
    [string] $Path,
    [string] $Name,
    [string] $DefaultValue
  )

  if (Test-Path -LiteralPath $Path) {
    $line = Get-Content -LiteralPath $Path | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -Last 1
    if ($line) {
      return ($line -split "=", 2)[1]
    }
  }
  return $DefaultValue
}

function Find-ComposeDirectory {
  param([string] $Root)

  $compose = Get-ChildItem -LiteralPath $Root -Filter "docker-compose.yml" -File -Recurse |
    Sort-Object { $_.FullName.Length } |
    Select-Object -First 1
  if (-not $compose) {
    throw "Downloaded release bundle does not contain docker-compose.yml. The release asset is incomplete."
  }
  return $compose.Directory.FullName
}

function Invoke-BuildingChangeInstaller {
  param(
    [string] $RequestedInstallDir,
    [string] $RequestedVersion = "latest",
    [switch] $DoNotStart,
    [switch] $DoNotCheckHealth,
    [switch] $AllowExisting,
    [switch] $PlanOnly,
    [string] $RequestedAssetName = "building-change-app.zip"
  )

  $target = Resolve-InstallDirectory -RequestedPath $RequestedInstallDir -AllowExisting:$AllowExisting
  $releaseUrl = Get-ReleaseApiUrl -RequestedVersion $RequestedVersion

  if ($PlanOnly) {
    Write-Host "Dry run: no files will be downloaded or extracted and Docker will not be started."
    Write-Host "Release metadata: $releaseUrl"
    Write-Host "Release asset: $RequestedAssetName"
    Write-Host "Installation directory: $target"
    Write-Host "Start Docker Compose: $(-not $DoNotStart)"
    return [pscustomobject]@{
      InstallDirectory = $target
      ReleaseApiUrl = $releaseUrl
      Downloaded = $false
      Extracted = $false
      DockerStarted = $false
    }
  }

  Test-DockerPrerequisites

  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  } catch {
    throw "Unable to enable TLS 1.2 for GitHub downloads. Update PowerShell or Windows and retry."
  }

  Write-Host "Resolving GitHub release '$RequestedVersion'..."
  try {
    $release = Invoke-RestMethod -Uri $releaseUrl -Headers @{ "User-Agent" = "building-change-windows-installer" }
  } catch {
    throw "Unable to resolve GitHub release '$RequestedVersion'. Check internet access and the release tag, then retry."
  }
  $asset = Get-ReleaseAsset -Release $release -Name $RequestedAssetName

  $createdTarget = -not (Test-Path -LiteralPath $target)
  if ($createdTarget) {
    New-Item -ItemType Directory -Path $target -Force | Out-Null
  }
  $downloadPath = Join-Path ([IO.Path]::GetTempPath()) ("building-change-" + [guid]::NewGuid().ToString("N") + ".zip")

  try {
    Write-Host "Downloading $RequestedAssetName..."
    try {
      Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $downloadPath -UseBasicParsing
    } catch {
      throw "Release download failed. Check internet access and confirm '$RequestedAssetName' is publicly accessible."
    }

    Write-Host "Extracting release..."
    Expand-Archive -LiteralPath $downloadPath -DestinationPath $target -Force:$AllowExisting
    $appDirectory = Find-ComposeDirectory -Root $target
    $envPath = Join-Path $appDirectory ".env"
    if (-not (Test-Path -LiteralPath $envPath)) {
      throw "Downloaded release bundle is incomplete: .env was not found beside docker-compose.yml."
    }

    if (-not $DoNotStart) {
      Push-Location $appDirectory
      try {
        $startScript = Join-Path $appDirectory "scripts\windows\start.ps1"
        if (Test-Path -LiteralPath $startScript) {
          & $startScript
        } else {
          docker compose --env-file .env up -d
          if ($LASTEXITCODE -ne 0) {
            throw "Docker Compose failed to start the application. Run 'docker compose --env-file .env logs' for details."
          }
        }

        if (-not $DoNotCheckHealth) {
          $healthScript = Join-Path $appDirectory "scripts\windows\health.ps1"
          if (Test-Path -LiteralPath $healthScript) {
            & $healthScript
          } else {
            Invoke-RestMethod -Uri "$script:AppUrl/" -TimeoutSec 30 | Out-Null
            Invoke-RestMethod -Uri "$script:AppUrl/api/health" -TimeoutSec 30 | Out-Null
          }

          $runtimeScript = Join-Path $appDirectory "scripts\windows\validate-runtime.ps1"
          if (Test-Path -LiteralPath $runtimeScript) {
            & $runtimeScript
          }
        }
      } finally {
        Pop-Location
      }
    }

    $frontendPort = Get-EnvironmentValue -Path $envPath -Name "FRONTEND_PORT" -DefaultValue "8080"
    $backendPort = Get-EnvironmentValue -Path $envPath -Name "BACKEND_PORT" -DefaultValue "8000"
    Write-Host ""
    Write-Host "Installation directory: $appDirectory"
    Write-Host "Application: http://127.0.0.1:$frontendPort"
    Write-Host "API documentation: http://127.0.0.1:$backendPort/docs"
    Write-Host "Health check: .\scripts\windows\health.ps1"
    Write-Host "Stop application: .\scripts\windows\stop.ps1"
  } catch {
    if ($createdTarget -and (Test-Path -LiteralPath $target)) {
      Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
  } finally {
    Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue
  }
}

if ($env:BUILDING_CHANGE_INSTALLER_TEST_MODE -ne "1") {
  Invoke-BuildingChangeInstaller `
    -RequestedInstallDir $InstallDir `
    -RequestedVersion $Version `
    -DoNotStart:$NoStart `
    -DoNotCheckHealth:$SkipHealth `
    -AllowExisting:$Force `
    -PlanOnly:$DryRun `
    -RequestedAssetName $AssetName
}
