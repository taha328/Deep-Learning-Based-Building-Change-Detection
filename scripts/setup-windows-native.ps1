[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$ForceEnv,
    [switch]$ForceModelDownload,
    [switch]$SkipFrontendBuild,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path -LiteralPath $RepoDir).Path
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$ScriptsDir = Join-Path $RepoRoot "scripts"
$LogsDir = Join-Path $RepoRoot "logs"
$ModelsDir = Join-Path $RepoRoot "models\bandon"
$ModelPath = Join-Path $ModelsDir "mtgcdnet_iter_40000.pth"
$LogPath = Join-Path $LogsDir "setup-windows-native.log"

$AppDbName = "building_change_app"
$AppDbUser = "building_change_user"
$AppDbPassword = "building_change_password"
$DatabaseUrl = "postgresql+psycopg://$AppDbUser`:$AppDbPassword@localhost:5432/$AppDbName"
$Script:PsqlPath = $null

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
New-Item -ItemType File -Force -Path $LogPath | Out-Null

function Redact-Message {
    param([string]$Message)
    $redacted = $Message -replace 'postgresql(\+psycopg)?://([^:]+):[^@]+@', 'postgresql$1://$2:***@'
    $redacted = $redacted -replace 'PGPASSWORD=[^ ]+', 'PGPASSWORD=***'
    $redacted = $redacted -replace 'pk\.[A-Za-z0-9._-]+', 'pk.***'
    return $redacted
}

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), (Redact-Message $Message)
    Add-Content -LiteralPath $LogPath -Value $line
    Write-Host $line
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    Write-Log ""
    Write-Log "== $Name =="
    try {
        & $Action
        Write-Log "OK: $Name"
    }
    catch {
        Write-Log "FAILED: $Name"
        Write-Log $_.Exception.Message
        Write-Log "See log: $LogPath"
        throw
    }
}

function ConvertFrom-SecureStringToPlainText {
    param([Parameter(Mandatory = $true)][Security.SecureString]$SecureString)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $RepoRoot,
        [hashtable]$Environment = @{}
    )

    Write-Log ("Running: {0} {1}" -f $FilePath, ($Arguments -join " "))
    $oldEnv = @{}
    foreach ($key in $Environment.Keys) {
        $oldEnv[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key], "Process")
    }

    try {
        Push-Location -LiteralPath $WorkingDirectory
        try {
            $output = & $FilePath @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }

        foreach ($line in @($output)) {
            if ($null -ne $line) {
                Write-Log ($line.ToString())
            }
        }

        if ($exitCode -ne 0) {
            throw "Command failed with exit code $exitCode`: $FilePath $($Arguments -join ' ')"
        }
        return $output
    }
    finally {
        foreach ($key in $Environment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $oldEnv[$key], "Process")
        }
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

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    $path = Get-CommandPath $Name
    if (-not $path) {
        throw "$Name is not available on PATH."
    }
    return $path
}

function Refresh-ProcessPath {
    $pathParts = @(
        $env:PATH,
        [Environment]::GetEnvironmentVariable("Path", "Machine"),
        [Environment]::GetEnvironmentVariable("Path", "User")
    ) | Where-Object { $_ }
    $env:PATH = ($pathParts -join ";")
}

function Test-CommandResult {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    try {
        & $FilePath @Arguments > $null 2>&1
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Verify
    )

    Refresh-ProcessPath
    if (& $Verify) {
        Write-Log "$Name already available."
        return
    }

    $null = Assert-Command "winget"
    Invoke-External "winget" @(
        "install",
        "--id", $Id,
        "--exact",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity"
    )

    Refresh-ProcessPath
    if (-not (& $Verify)) {
        throw "$Name install completed but verification failed. Install or repair $Name, then rerun this script."
    }
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

function Wait-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$ComputerName,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 45
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort -ComputerName $ComputerName -Port $Port) {
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
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

function Test-PostgreSql16Client {
    $psql = Find-Psql
    if (-not $psql) {
        return $false
    }
    $version = (& $psql --version 2>$null) -join " "
    return $version -match "\b16(\.|$)"
}

function Add-PostgreSqlToPath {
    $psql = Find-Psql
    if (-not $psql) {
        return
    }
    $binDir = Split-Path -Parent $psql
    if (($env:PATH -split ";") -notcontains $binDir) {
        $env:PATH = "$binDir;$env:PATH"
    }
    $Script:PsqlPath = $psql
}

function Ensure-PostgreSqlService {
    $services = @(Get-Service | Where-Object { $_.Name -like "postgresql*" -or $_.DisplayName -like "PostgreSQL*" })
    foreach ($service in $services) {
        if ($service.Status -ne "Running") {
            Write-Log "Starting service: $($service.Name)"
            Start-Service -Name $service.Name
        }
    }

    if (-not (Wait-TcpPort -ComputerName "127.0.0.1" -Port 5432 -TimeoutSeconds 45)) {
        throw "PostgreSQL is not accepting TCP connections on 127.0.0.1:5432."
    }
}

function Quote-SqlLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("'", "''")
}

function Assert-SafeSqlIdentifier {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "Unsafe SQL identifier: $Value"
    }
}

function Invoke-Psql {
    param(
        [string]$Database = "postgres",
        [Parameter(Mandatory = $true)][string]$Sql,
        [string]$User = "postgres",
        [Security.SecureString]$Password = $null,
        [switch]$TuplesOnly
    )

    if (-not $Script:PsqlPath) {
        $Script:PsqlPath = Find-Psql
    }
    if (-not $Script:PsqlPath) {
        throw "psql.exe was not found."
    }

    $oldPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")
    $plainPassword = $null
    try {
        if ($Password) {
            $plainPassword = ConvertFrom-SecureStringToPlainText $Password
            [Environment]::SetEnvironmentVariable("PGPASSWORD", $plainPassword, "Process")
        }
        else {
            [Environment]::SetEnvironmentVariable("PGPASSWORD", "", "Process")
        }

        $args = @(
            "-h", "127.0.0.1",
            "-p", "5432",
            "-U", $User,
            "-d", $Database,
            "-v", "ON_ERROR_STOP=1",
            "-w"
        )
        if ($TuplesOnly) {
            $args += @("-t", "-A")
        }
        $args += @("-c", $Sql)

        Write-Log "Running psql against database=$Database user=$User"
        $output = & $Script:PsqlPath @args 2>&1
        $exitCode = $LASTEXITCODE
        foreach ($line in @($output)) {
            if ($null -ne $line) {
                Write-Log ($line.ToString())
            }
        }
        if ($exitCode -ne 0) {
            throw "psql failed with exit code $exitCode."
        }
        return (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
    }
    finally {
        [Environment]::SetEnvironmentVariable("PGPASSWORD", $oldPassword, "Process")
        $plainPassword = $null
    }
}

function Get-PostgresAdminPassword {
    try {
        $null = Invoke-Psql -Database "postgres" -Sql "SELECT 1;" -User "postgres" -TuplesOnly
        Write-Log "Connected to PostgreSQL as postgres without prompting."
        return $null
    }
    catch {
        Write-Log "PostgreSQL requires the local postgres password."
        $password = Read-Host "Enter the local PostgreSQL 'postgres' password" -AsSecureString
        $null = Invoke-Psql -Database "postgres" -Sql "SELECT 1;" -User "postgres" -Password $password -TuplesOnly
        return $password
    }
}

function Ensure-ApplicationDatabase {
    param([Security.SecureString]$AdminPassword = $null)

    Assert-SafeSqlIdentifier $AppDbName
    Assert-SafeSqlIdentifier $AppDbUser
    $escapedPassword = Quote-SqlLiteral $AppDbPassword

    $roleSql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$AppDbUser') THEN
        CREATE ROLE "$AppDbUser" LOGIN PASSWORD '$escapedPassword';
    ELSE
        ALTER ROLE "$AppDbUser" WITH LOGIN PASSWORD '$escapedPassword';
    END IF;
END
`$`$;
"@
    $null = Invoke-Psql -Database "postgres" -Sql $roleSql -User "postgres" -Password $AdminPassword

    $databaseExists = Invoke-Psql -Database "postgres" -Sql "SELECT 1 FROM pg_database WHERE datname = '$AppDbName';" -User "postgres" -Password $AdminPassword -TuplesOnly
    if ($databaseExists -notmatch "1") {
        $null = Invoke-Psql -Database "postgres" -Sql "CREATE DATABASE `"$AppDbName`" OWNER `"$AppDbUser`";" -User "postgres" -Password $AdminPassword
    }
    else {
        Write-Log "Database already exists: $AppDbName"
    }

    $grantSql = @"
GRANT ALL PRIVILEGES ON DATABASE "$AppDbName" TO "$AppDbUser";
GRANT USAGE, CREATE ON SCHEMA public TO "$AppDbUser";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "$AppDbUser";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "$AppDbUser";
"@
    $null = Invoke-Psql -Database $AppDbName -Sql $grantSql -User "postgres" -Password $AdminPassword
}

function Install-PostGisBundle {
    $pgMajor = "16"
    $indexUri = "https://download.osgeo.org/postgis/windows/pg$pgMajor/"
    Write-Log "Searching for PostGIS bundle at $indexUri"
    $response = Invoke-WebRequest -Uri $indexUri -UseBasicParsing
    $pattern = "postgis-bundle-pg$pgMajor" + 'x64-setup-[^"''<>]+\.exe'
    $matches = [regex]::Matches($response.Content, $pattern)
    if ($matches.Count -eq 0) {
        throw "Could not locate a PostGIS bundle for PostgreSQL $pgMajor. Install PostGIS with StackBuilder, then rerun setup."
    }

    $installerName = @($matches | ForEach-Object { $_.Value } | Sort-Object -Descending | Select-Object -First 1)[0]
    $downloadUri = "$indexUri$installerName"
    $installerPath = Join-Path ([IO.Path]::GetTempPath()) $installerName
    Write-Log "Downloading PostGIS bundle: $downloadUri"
    Invoke-WebRequest -Uri $downloadUri -OutFile $installerPath -UseBasicParsing

    Write-Log "Installing PostGIS bundle silently."
    $process = Start-Process -FilePath $installerPath -ArgumentList "/S" -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "PostGIS bundle installer failed with exit code $($process.ExitCode)."
    }
}

function Ensure-PostGis {
    param([Security.SecureString]$AdminPassword = $null)

    try {
        $null = Invoke-Psql -Database $AppDbName -Sql "CREATE EXTENSION IF NOT EXISTS postgis; SELECT PostGIS_Full_Version();" -User "postgres" -Password $AdminPassword
    }
    catch {
        Write-Log "PostGIS extension is not available yet. Attempting PostGIS bundle install."
        Install-PostGisBundle
        $null = Invoke-Psql -Database $AppDbName -Sql "CREATE EXTENSION IF NOT EXISTS postgis; SELECT PostGIS_Full_Version();" -User "postgres" -Password $AdminPassword
    }

    try {
        $null = Invoke-Psql -Database $AppDbName -Sql "CREATE EXTENSION IF NOT EXISTS postgis_topology;" -User "postgres" -Password $AdminPassword
    }
    catch {
        Write-Log "PostGIS topology extension was not enabled. Continuing with core PostGIS."
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

function Ensure-Memurai {
    if (Test-RedisPing) {
        Write-Log "Redis-compatible service already responds to PING on 127.0.0.1:6379."
        return
    }

    Ensure-WingetPackage -Id "Memurai.MemuraiDeveloper" -Name "Memurai Developer" -Verify {
        Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "Memurai*" -or $_.DisplayName -like "Memurai*" } | Select-Object -First 1
    }

    $services = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "Memurai*" -or $_.DisplayName -like "Memurai*" })
    foreach ($service in $services) {
        if ($service.Status -ne "Running") {
            Write-Log "Starting service: $($service.Name)"
            Start-Service -Name $service.Name
        }
    }

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-RedisPing) {
            Write-Log "Memurai responds to PING."
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "No Redis-compatible service responded on 127.0.0.1:6379 after installing or starting Memurai."
}

function Write-EnvFromTemplate {
    param(
        [Parameter(Mandatory = $true)][string]$TemplatePath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    if ((Test-Path -LiteralPath $TargetPath) -and -not $ForceEnv) {
        Write-Log "Preserving existing env file: $TargetPath"
        return
    }

    if (Test-Path -LiteralPath $TargetPath) {
        $backupPath = "$TargetPath.$(Get-Date -Format 'yyyyMMddHHmmss').bak"
        Copy-Item -LiteralPath $TargetPath -Destination $backupPath
        Write-Log "Backed up existing env file to $backupPath"
    }

    $content = [IO.File]::ReadAllText($TemplatePath)
    $content = $content.Replace("__REPO_ROOT__", $RepoRoot)
    [IO.File]::WriteAllText($TargetPath, $content, [System.Text.UTF8Encoding]::new($false))
    Write-Log "Wrote env file: $TargetPath"
}

function Test-ModelFile {
    if (-not (Test-Path -LiteralPath $ModelPath)) {
        return $false
    }
    $item = Get-Item -LiteralPath $ModelPath
    return $item.Length -gt 10MB
}

function Get-ModelReleaseAsset {
    $apiUri = "https://api.github.com/repos/taha328/Deep-Learning-Based-Building-Change-Detection/releases?per_page=20"
    $headers = @{ "User-Agent" = "building-change-windows-native-setup" }
    $releases = @(Invoke-RestMethod -Uri $apiUri -Headers $headers)

    foreach ($release in $releases) {
        foreach ($asset in @($release.assets)) {
            if ($asset.name -eq "mtgcdnet_iter_40000.pth") {
                return [pscustomobject]@{ Name = $asset.name; Url = $asset.browser_download_url; Kind = "pth" }
            }
        }
    }

    foreach ($release in $releases) {
        foreach ($asset in @($release.assets)) {
            if ($asset.name -match "(bandon|mtgcdnet|model)" -and $asset.name -match "\.zip$") {
                return [pscustomobject]@{ Name = $asset.name; Url = $asset.browser_download_url; Kind = "zip" }
            }
        }
    }

    throw "Could not find mtgcdnet_iter_40000.pth or a model zip in recent GitHub Releases."
}

function Ensure-ModelArtifact {
    if ((Test-ModelFile) -and -not $ForceModelDownload) {
        $sizeMb = [math]::Round((Get-Item -LiteralPath $ModelPath).Length / 1MB, 1)
        Write-Log "Model already present: $ModelPath ($sizeMb MB)"
        return
    }

    New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
    $asset = Get-ModelReleaseAsset
    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("building-change-model-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

    try {
        $downloadPath = Join-Path $tempRoot $asset.Name
        Write-Log "Downloading model asset: $($asset.Name)"
        Invoke-WebRequest -Uri $asset.Url -OutFile $downloadPath -UseBasicParsing

        if ($asset.Kind -eq "pth") {
            Copy-Item -LiteralPath $downloadPath -Destination $ModelPath -Force
        }
        else {
            $extractDir = Join-Path $tempRoot "extract"
            New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
            Expand-Archive -LiteralPath $downloadPath -DestinationPath $extractDir -Force
            $candidate = Get-ChildItem -LiteralPath $extractDir -Recurse -Filter "mtgcdnet_iter_40000.pth" |
                Select-Object -First 1
            if (-not $candidate) {
                throw "Downloaded model archive does not contain mtgcdnet_iter_40000.pth."
            }
            Copy-Item -LiteralPath $candidate.FullName -Destination $ModelPath -Force
        }
    }
    finally {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-ModelFile)) {
        throw "Model download verification failed. Expected $ModelPath to exist and be larger than 10 MB."
    }
    $sizeMb = [math]::Round((Get-Item -LiteralPath $ModelPath).Length / 1MB, 1)
    Write-Log "Model ready: $ModelPath ($sizeMb MB)"
}

function Ensure-BackendVenv {
    $venvDir = Join-Path $BackendDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-External "py" @("-3.11", "-m", "venv", $venvDir) -WorkingDirectory $BackendDir
    }

    $venvScripts = Join-Path $venvDir "Scripts"
    $envBlock = @{
        "PYTHONNOUSERSITE" = "1"
        "PATH" = "$venvScripts;$env:PATH"
    }
    Invoke-External $venvPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel") -WorkingDirectory $BackendDir -Environment $envBlock
    Invoke-External $venvPython @("-m", "pip", "install", "-r", (Join-Path $BackendDir "requirements.txt")) -WorkingDirectory $BackendDir -Environment $envBlock
}

function Invoke-BackendImportCheck {
    $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
    $venvScripts = Join-Path $BackendDir ".venv\Scripts"
    $envBlock = @{
        "PYTHONNOUSERSITE" = "1"
        "PATH" = "$venvScripts;$env:PATH"
        "DATABASE_URL" = $DatabaseUrl
        "PERSISTENCE_BACKEND" = "postgres"
    }
    Invoke-External $venvPython @(
        "-c",
        "from src.api.main import app; from src.jobs.celery_app import celery_app; print('backend-import-ok')"
    ) -WorkingDirectory $BackendDir -Environment $envBlock
}

function Invoke-DatabaseMigrations {
    $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
    $venvScripts = Join-Path $BackendDir ".venv\Scripts"
    $envBlock = @{
        "PYTHONNOUSERSITE" = "1"
        "PATH" = "$venvScripts;$env:PATH"
        "DATABASE_URL" = $DatabaseUrl
        "PERSISTENCE_BACKEND" = "postgres"
    }
    Invoke-External $venvPython @(
        "scripts\setup_postgis_db.py",
        "--database-url", $DatabaseUrl,
        "--migrate",
        "--verify"
    ) -WorkingDirectory $BackendDir -Environment $envBlock
}

function Ensure-FrontendDependencies {
    $null = Assert-Command "npm"
    if (Test-Path -LiteralPath (Join-Path $FrontendDir "package-lock.json")) {
        Invoke-External "npm" @("ci") -WorkingDirectory $FrontendDir
    }
    else {
        Invoke-External "npm" @("install") -WorkingDirectory $FrontendDir
    }

    if (-not $SkipFrontendBuild) {
        Invoke-External "npm" @("run", "build") -WorkingDirectory $FrontendDir
    }
}

if (-not (Test-IsAdministrator)) {
    throw "Run this script from an elevated PowerShell session: right-click PowerShell and choose 'Run as administrator'."
}

Invoke-Step "Verify repository layout" {
    if (-not (Test-Path -LiteralPath (Join-Path $BackendDir "requirements.txt"))) {
        throw "backend/requirements.txt not found under $RepoRoot"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir "package.json"))) {
        throw "frontend/package.json not found under $RepoRoot"
    }
}

Invoke-Step "Install and verify core tools" {
    $null = Assert-Command "winget"
    Ensure-WingetPackage -Id "Git.Git" -Name "Git" -Verify { Test-CommandResult "git" @("--version") }
    Ensure-WingetPackage -Id "Python.Python.3.11" -Name "Python 3.11" -Verify { Test-CommandResult "py" @("-3.11", "--version") }
    Ensure-WingetPackage -Id "OpenJS.NodeJS.LTS" -Name "Node.js LTS" -Verify {
        (Test-CommandResult "node" @("--version")) -and (Test-CommandResult "npm" @("--version"))
    }
}

Invoke-Step "Install and verify PostgreSQL 16" {
    Ensure-WingetPackage -Id "PostgreSQL.PostgreSQL.16" -Name "PostgreSQL 16" -Verify { Test-PostgreSql16Client }
    Add-PostgreSqlToPath
    Ensure-PostgreSqlService
}

Invoke-Step "Install and verify Redis-compatible Memurai" {
    Ensure-Memurai
}

$AdminPassword = $null
Invoke-Step "Provision PostgreSQL database and PostGIS" {
    $AdminPassword = Get-PostgresAdminPassword
    Ensure-ApplicationDatabase -AdminPassword $AdminPassword
    Ensure-PostGis -AdminPassword $AdminPassword
}

Invoke-Step "Write native env files" {
    Write-EnvFromTemplate -TemplatePath (Join-Path $BackendDir ".env.windows.example") -TargetPath (Join-Path $BackendDir ".env")
    Write-EnvFromTemplate -TemplatePath (Join-Path $FrontendDir ".env.local.windows.example") -TargetPath (Join-Path $FrontendDir ".env.local")
}

Invoke-Step "Create backend virtualenv and install backend dependencies" {
    Ensure-BackendVenv
}

Invoke-Step "Install frontend dependencies and build frontend" {
    Ensure-FrontendDependencies
}

Invoke-Step "Download or verify BANDON model artifact" {
    Ensure-ModelArtifact
}

Invoke-Step "Run backend import check" {
    Invoke-BackendImportCheck
}

Invoke-Step "Run PostgreSQL migrations" {
    Invoke-DatabaseMigrations
}

Invoke-Step "Run native health check" {
    Invoke-External "powershell.exe" @(
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $ScriptsDir "health-windows-native.ps1"),
        "-RepoDir", $RepoRoot
    ) -WorkingDirectory $RepoRoot
}

if (-not $NoStart) {
    Invoke-Step "Start native services" {
        Invoke-External "powershell.exe" @(
            "-ExecutionPolicy", "Bypass",
            "-File", (Join-Path $ScriptsDir "start-windows-native.ps1"),
            "-RepoDir", $RepoRoot
        ) -WorkingDirectory $RepoRoot
    }
}

Write-Log ""
Write-Log "Native Windows setup complete."
Write-Host ""
Write-Host "Open the app at http://127.0.0.1:5173"
Write-Host "Open API docs at http://127.0.0.1:8000/docs"
Write-Host ""
Write-Host "Start later: powershell -ExecutionPolicy Bypass -File scripts\start-windows-native.ps1"
Write-Host "Health:      powershell -ExecutionPolicy Bypass -File scripts\health-windows-native.ps1"
Write-Host "Stop:        powershell -ExecutionPolicy Bypass -File scripts\stop-windows-native.ps1"
