[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$StopServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path -LiteralPath $RepoDir).Path
$LogsDir = Join-Path $RepoRoot "logs"
$ProcessFile = Join-Path $LogsDir "native-processes.json"

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

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $children = @()
    try {
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction Stop)
    }
    catch {
        $children = @()
    }

    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force
    }
}

$recorded = @(Get-RecordedProcesses)
if ($recorded.Count -eq 0) {
    Write-Host "No native app process manifest found at $ProcessFile."
}
else {
    foreach ($entry in $recorded) {
        if (-not $entry.pid) {
            continue
        }
        $name = if ($entry.name) { $entry.name } else { "unknown" }
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if (-not $process) {
            Write-Host "Already stopped: $name pid=$($entry.pid)"
            continue
        }
        Write-Host "Stopping $name pid=$($entry.pid)"
        Stop-ProcessTree -ProcessId ([int]$entry.pid)
    }
    Remove-Item -LiteralPath $ProcessFile -Force -ErrorAction SilentlyContinue
    Write-Host "Removed native process manifest: $ProcessFile"
}

if ($StopServices) {
    $services = @(Get-Service | Where-Object {
        $_.Name -like "postgresql*" -or
        $_.DisplayName -like "PostgreSQL*" -or
        $_.Name -like "Memurai*" -or
        $_.DisplayName -like "Memurai*"
    })
    foreach ($service in $services) {
        if ($service.Status -eq "Running") {
            Write-Host "Stopping service: $($service.Name)"
            Stop-Service -Name $service.Name
        }
    }
}
else {
    Write-Host "PostgreSQL and Memurai services were left running. Pass -StopServices to stop them explicitly."
}
