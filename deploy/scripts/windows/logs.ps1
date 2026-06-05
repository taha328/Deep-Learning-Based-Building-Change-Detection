param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $Service
)

$ErrorActionPreference = "Stop"

$DeployDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $DeployDir

if ($Service.Count -gt 0) {
  docker compose --env-file .env logs --tail=200 @Service
} else {
  docker compose --env-file .env logs --tail=200
}
