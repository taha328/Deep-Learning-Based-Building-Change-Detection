[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path -LiteralPath $RepoDir).Path
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$LogsDir = Join-Path $RepoRoot "logs"
$ProcessFile = Join-Path $LogsDir "native-processes.json"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$VenvScripts = Join-Path $BackendDir ".venv\Scripts"
$ModelPath = Join-Path $RepoRoot "models\bandon\mtgcdnet_iter_40000.pth"

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Get-PowerShellExe {
    $windowsPowerShell = Get-Command "powershell.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($windowsPowerShell) {
        return $windowsPowerShell.Source
    }
    $pwsh = Get-Command "pwsh" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pwsh) {
        return $pwsh.Source
    }
    throw "Neither powershell.exe nor pwsh is available on PATH."
}

function Get-RecordedProcesses {
    if (-not (Test-Path -LiteralPath $ProcessFile)) {
        return @()
    }
    $content = Get-Content -LiteralPath $ProcessFile -Raw
    if (-not $content.Trim()) {
        return @()
    }
    return @($content | ConvertFrom-Json)
}

function Assert-NoRecordedProcessesRunning {
    $running = @()
    foreach ($entry in (Get-RecordedProcesses)) {
        if ($entry.pid -and (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)) {
            $running += $entry
        }
    }
    if ($running.Count -gt 0) {
        $names = ($running | ForEach-Object { "$($_.name) pid=$($_.pid)" }) -join ", "
        throw "Native app processes are already recorded as running: $names. Run scripts\stop-windows-native.ps1 first."
    }
}

function Write-HelperScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    [IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Start-NativeWindow {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $powerShellExe = Get-PowerShellExe
    $process = Start-Process -FilePath $powerShellExe -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-File", $ScriptPath
    ) -WorkingDirectory $WorkingDirectory -PassThru

    return [pscustomobject]@{
        name = $Name
        pid = $process.Id
        command = "$powerShellExe -NoExit -ExecutionPolicy Bypass -File `"$ScriptPath`""
        cwd = $WorkingDirectory
        startedAt = (Get-Date).ToString("o")
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Missing backend virtualenv: $VenvPython. Run scripts\setup-windows-native.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $BackendDir ".env"))) {
    throw "Missing backend\.env. Run scripts\setup-windows-native.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir ".env.local"))) {
    throw "Missing frontend\.env.local. Run scripts\setup-windows-native.ps1 first."
}
if (-not (Test-Path -LiteralPath $ModelPath) -or (Get-Item -LiteralPath $ModelPath).Length -le 10MB) {
    throw "Missing or incomplete BANDON model artifact: $ModelPath. Run scripts\setup-windows-native.ps1 first."
}

Assert-NoRecordedProcessesRunning

$backendScript = Join-Path $LogsDir "native-backend-api.ps1"
$workerScript = Join-Path $LogsDir "native-celery-worker.ps1"
$frontendScript = Join-Path $LogsDir "native-frontend.ps1"

$backendContent = @'
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Building Change API"
Set-Location -LiteralPath "__BACKEND_DIR__"
$env:APP_NATIVE_PROCESS_ROLE = "backend-api"
$env:PYTHONNOUSERSITE = "1"
$env:PATH = "__VENV_SCRIPTS__;$env:PATH"
& "__VENV_PYTHON__" -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir src --reload-exclude ".venv/*" --reload-exclude "__pycache__/*" --reload-exclude "*.pyc"
'@
$backendContent = $backendContent.Replace("__BACKEND_DIR__", $BackendDir).Replace("__VENV_SCRIPTS__", $VenvScripts).Replace("__VENV_PYTHON__", $VenvPython)
Write-HelperScript -Path $backendScript -Content $backendContent

$workerContent = @'
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Building Change Celery Worker"
Set-Location -LiteralPath "__BACKEND_DIR__"
$env:APP_NATIVE_PROCESS_ROLE = "celery-worker"
$env:PYTHONNOUSERSITE = "1"
$env:PATH = "__VENV_SCRIPTS__;$env:PATH"
& "__VENV_PYTHON__" -m celery -A src.jobs.celery_app.celery_app worker --loglevel=INFO --queues building_change --pool=solo
'@
$workerContent = $workerContent.Replace("__BACKEND_DIR__", $BackendDir).Replace("__VENV_SCRIPTS__", $VenvScripts).Replace("__VENV_PYTHON__", $VenvPython)
Write-HelperScript -Path $workerScript -Content $workerContent

$frontendContent = @'
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Building Change Frontend"
Set-Location -LiteralPath "__FRONTEND_DIR__"
$env:APP_NATIVE_PROCESS_ROLE = "frontend"
npm run dev
'@
$frontendContent = $frontendContent.Replace("__FRONTEND_DIR__", $FrontendDir)
Write-HelperScript -Path $frontendScript -Content $frontendContent

$processes = @()
$processes += Start-NativeWindow -Name "backend-api" -ScriptPath $backendScript -WorkingDirectory $BackendDir
$processes += Start-NativeWindow -Name "celery-worker" -ScriptPath $workerScript -WorkingDirectory $BackendDir
$processes += Start-NativeWindow -Name "frontend" -ScriptPath $frontendScript -WorkingDirectory $FrontendDir

$processes | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ProcessFile -Encoding UTF8

Write-Host "Started native Windows services."
Write-Host "Process manifest: $ProcessFile"
Write-Host "Frontend: http://127.0.0.1:5173"
Write-Host "API docs:  http://127.0.0.1:8000/docs"
