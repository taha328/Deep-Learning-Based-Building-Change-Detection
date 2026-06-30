[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path -LiteralPath $RepoDir).Path
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$BackendEnvPath = Join-Path $BackendDir ".env"
$FrontendEnvPath = Join-Path $FrontendDir ".env.local"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$VenvScripts = Join-Path $BackendDir ".venv\Scripts"
$Script:Failures = [System.Collections.Generic.List[string]]::new()
$Script:Warnings = [System.Collections.Generic.List[string]]::new()

function Add-Ok {
    param([string]$Name, [string]$Details = "")
    if ($Details) {
        Write-Host "[OK]   $Name - $Details"
    }
    else {
        Write-Host "[OK]   $Name"
    }
}

function Add-Warn {
    param([string]$Name, [string]$Details)
    $Script:Warnings.Add("$Name - $Details")
    Write-Host "[WARN] $Name - $Details"
}

function Add-Fail {
    param([string]$Name, [string]$Details)
    $Script:Failures.Add("$Name - $Details")
    Write-Host "[FAIL] $Name - $Details"
}

function Invoke-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    try {
        $details = & $Action
        Add-Ok -Name $Name -Details (($details | ForEach-Object { $_.ToString() }) -join " ")
    }
    catch {
        Add-Fail -Name $Name -Details $_.Exception.Message
    }
}

function Get-CommandPath {
    param([Parameter(Mandatory = $true)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        return $null
    }
    return $command.Source
}

function Get-PsqlCandidates {
    $candidates = @()
    $command = Get-CommandPath "psql.exe"
    if ($command) {
        $candidates += $command
    }
    $command = Get-CommandPath "psql"
    if ($command) {
        $candidates += $command
    }

    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($root in $roots) {
        $matches = Get-ChildItem -Path (Join-Path $root "PostgreSQL\*\bin\psql.exe") -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        foreach ($match in @($matches)) {
            $candidates += $match.FullName
        }
    }
    return @($candidates | Where-Object { $_ } | Select-Object -Unique)
}

function Find-Psql {
    $candidates = @(Get-PsqlCandidates)
    foreach ($candidate in $candidates) {
        $version = (& $candidate --version 2>$null) -join " "
        if ($version -match "\b16(\.|$)") {
            return $candidate
        }
    }
    if ($candidates.Count -gt 0) {
        return $candidates[0]
    }
    return $null
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$ComputerName,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 2000
    )

    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($ComputerName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $client.EndConnect($async)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $client) {
            $client.Close()
        }
    }
}

function Test-RedisPing {
    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.Connect("127.0.0.1", 6379)
        $stream = $client.GetStream()
        $payload = [System.Text.Encoding]::ASCII.GetBytes("*1`r`n`$4`r`nPING`r`n")
        $stream.Write($payload, 0, $payload.Length)
        $buffer = New-Object byte[] 128
        $count = $stream.Read($buffer, 0, $buffer.Length)
        $reply = [System.Text.Encoding]::ASCII.GetString($buffer, 0, $count)
        return $reply.StartsWith("+PONG")
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $client) {
            $client.Close()
        }
    }
}

function Read-EnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing env file: $Path"
    }

    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") {
            continue
        }
        $parts = $line.Split("=", 2)
        $values[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
    }
    return ,$values
}

function Assert-EnvKeys {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Values,
        [Parameter(Mandatory = $true)][string[]]$Keys
    )
    $missing = @()
    foreach ($key in $Keys) {
        if (-not $Values.ContainsKey($key) -or -not $Values[$key]) {
            $missing += $key
        }
    }
    if ($missing.Count -gt 0) {
        throw "Missing required keys: $($missing -join ', ')"
    }
    return "$($Keys.Count) required keys present"
}

function Assert-NoUnreplacedRepoRootPlaceholder {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing env file: $Path"
    }
    $content = [IO.File]::ReadAllText($Path)
    if ($content.Contains("__REPO_ROOT__")) {
        throw "Unreplaced __REPO_ROOT__ placeholder remains in $Path"
    }
}

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    $expanded = $Value.Replace("__REPO_ROOT__", $RepoRoot)
    if ([IO.Path]::IsPathRooted($expanded)) {
        return $expanded
    }
    return Join-Path $RepoRoot $expanded
}

function ConvertTo-PsqlUrl {
    param([Parameter(Mandatory = $true)][string]$DatabaseUrl)
    return ($DatabaseUrl -replace '^postgresql\+psycopg://', 'postgresql://')
}

function Invoke-WithProcessEnv {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    $oldEnv = @{}
    foreach ($key in $Environment.Keys) {
        $oldEnv[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key], "Process")
    }
    try {
        & $Action
    }
    finally {
        foreach ($key in $Environment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $oldEnv[$key], "Process")
        }
    }
}

$BackendEnv = @{}
$FrontendEnv = @{}
$PsqlPath = $null

Invoke-Check "Git" {
    $path = Get-CommandPath "git"
    if (-not $path) { throw "git not found" }
    (& $path --version 2>&1) -join " "
}

Invoke-Check "Python 3.11 launcher" {
    $path = Get-CommandPath "py"
    if (-not $path) { throw "py launcher not found" }
    $output = & $path -3.11 --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join " ") }
    ($output -join " ")
}

Invoke-Check "Node.js and npm" {
    $node = Get-CommandPath "node"
    $npm = Get-CommandPath "npm"
    if (-not $node) { throw "node not found" }
    if (-not $npm) { throw "npm not found" }
    "node=$((& $node --version 2>&1) -join ' ') npm=$((& $npm --version 2>&1) -join ' ')"
}

Invoke-Check "psql client" {
    $script:PsqlPath = Find-Psql
    if (-not $script:PsqlPath) { throw "psql.exe not found" }
    $version = (& $script:PsqlPath --version 2>&1) -join " "
    if ($version -notmatch "\b16(\.|$)") {
        Add-Warn "psql client" "Expected PostgreSQL 16 client, got: $version"
    }
    $version
}

Invoke-Check "PostgreSQL TCP port" {
    if (-not (Test-TcpPort -ComputerName "127.0.0.1" -Port 5432)) {
        throw "127.0.0.1:5432 is not accepting connections"
    }
    "127.0.0.1:5432 is open"
}

Invoke-Check "Backend env file" {
    Assert-NoUnreplacedRepoRootPlaceholder -Path $BackendEnvPath
    $script:BackendEnv = Read-EnvFile -Path $BackendEnvPath
    Assert-EnvKeys -Values $script:BackendEnv -Keys @(
        "PERSISTENCE_BACKEND",
        "DATABASE_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "APP_INFERENCE_BACKEND",
        "APP_BANDON_REPO_DIR",
        "APP_BANDON_ENV_PREFIX",
        "APP_BANDON_CONFIG_PATH",
        "APP_BANDON_CHECKPOINT_PATH",
        "MAPBOX_ACCESS_TOKEN",
        "APP_WAYBACK_DEFAULT_ZOOM",
        "APP_TILE_ZOOM"
    )
}

Invoke-Check "Frontend env file" {
    Assert-NoUnreplacedRepoRootPlaceholder -Path $FrontendEnvPath
    $script:FrontendEnv = Read-EnvFile -Path $FrontendEnvPath
    Assert-EnvKeys -Values $script:FrontendEnv -Keys @(
        "VITE_FRONTEND_MODE",
        "VITE_FASTAPI_BACKEND_URL",
        "VITE_MAPBOX_API_KEY"
    )
}

Invoke-Check "PostgreSQL database and PostGIS" {
    if (-not $script:PsqlPath) {
        $script:PsqlPath = Find-Psql
    }
    if (-not $script:PsqlPath) { throw "psql.exe not found" }
    if (-not $script:BackendEnv.ContainsKey("DATABASE_URL")) { throw "DATABASE_URL missing" }
    $psqlUrl = ConvertTo-PsqlUrl $script:BackendEnv["DATABASE_URL"]
    $output = & $script:PsqlPath $psqlUrl -v ON_ERROR_STOP=1 -t -A -c "SELECT current_database(); SELECT PostGIS_Full_Version();" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($output -join " ")
    }
    ($output | Where-Object { $_.ToString().Trim() } | Select-Object -First 2) -join " | "
}

Invoke-Check "Redis-compatible PING" {
    if (-not (Test-RedisPing)) {
        throw "No PONG from 127.0.0.1:6379"
    }
    "PONG"
}

Invoke-Check "Backend virtualenv" {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "Missing $VenvPython"
    }
    $output = & $VenvPython --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($output -join " ")
    }
    ($output -join " ")
}

Invoke-Check "BANDON model artifact" {
    if (-not $script:BackendEnv.ContainsKey("APP_BANDON_CHECKPOINT_PATH")) {
        throw "APP_BANDON_CHECKPOINT_PATH missing"
    }
    $modelPath = Resolve-RepoPath $script:BackendEnv["APP_BANDON_CHECKPOINT_PATH"]
    if (-not (Test-Path -LiteralPath $modelPath)) {
        throw "Missing model: $modelPath"
    }
    $item = Get-Item -LiteralPath $modelPath
    if ($item.Length -le 10MB) {
        throw "Model file is unexpectedly small: $($item.Length) bytes"
    }
    "$([math]::Round($item.Length / 1MB, 1)) MB"
}

Invoke-Check "Backend import" {
    $envBlock = @{
        "PYTHONNOUSERSITE" = "1"
        "PATH" = "$VenvScripts;$env:PATH"
    }
    Invoke-WithProcessEnv -Environment $envBlock -Action {
        Push-Location -LiteralPath $BackendDir
        try {
            $output = & $VenvPython -c "from src.api.main import app; from src.jobs.celery_app import celery_app; print('backend-import-ok')" 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw ($output -join " ")
            }
            ($output -join " ")
        }
        finally {
            Pop-Location
        }
    }
}

Invoke-Check "Alembic migrations table" {
    if (-not $script:PsqlPath) {
        $script:PsqlPath = Find-Psql
    }
    if (-not $script:PsqlPath) { throw "psql.exe not found" }
    $psqlUrl = ConvertTo-PsqlUrl $script:BackendEnv["DATABASE_URL"]
    $output = & $script:PsqlPath $psqlUrl -v ON_ERROR_STOP=1 -t -A -c "SELECT to_regclass('public.alembic_version');" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($output -join " ")
    }
    if (($output -join " ").Trim() -ne "alembic_version") {
        throw "alembic_version table is missing"
    }
    "alembic_version present"
}

if (Test-TcpPort -ComputerName "127.0.0.1" -Port 8000 -TimeoutMilliseconds 500) {
    Invoke-Check "API health endpoint" {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 5 -UseBasicParsing
        "HTTP $($response.StatusCode)"
    }
}
else {
    Add-Warn "API health endpoint" "API is not running on 127.0.0.1:8000; start it with scripts\start-windows-native.ps1"
}

if (Test-TcpPort -ComputerName "127.0.0.1" -Port 5173 -TimeoutMilliseconds 500) {
    Invoke-Check "Frontend endpoint" {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:5173" -TimeoutSec 5 -UseBasicParsing
        "HTTP $($response.StatusCode)"
    }
}
else {
    Add-Warn "Frontend endpoint" "frontend is not running on 127.0.0.1:5173; start it with scripts\start-windows-native.ps1"
}

Write-Host ""
Write-Host "Health summary: $($Script:Failures.Count) failure(s), $($Script:Warnings.Count) warning(s)."
if ($Script:Failures.Count -gt 0) {
    exit 1
}
exit 0
