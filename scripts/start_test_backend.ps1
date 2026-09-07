param(
    [string]$PublicHost = "127.0.0.1",
    [ValidateSet("8081", "8082")]
    [Parameter(Mandatory = $true)]
    [string]$TestBackendPort,
    [string]$AdminUsername = "admin",
    [string]$AdminPassword = $env:TONGTONG_ADMIN_PASSWORD
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$exampleConfig = Join-Path $repoRoot "backend/config.test.example.yaml"
$runtimeConfig = Join-Path $repoRoot "backend/config.test.yaml"

if (-not $AdminPassword) {
    throw "Set TONGTONG_ADMIN_PASSWORD or pass -AdminPassword (minimum 8 characters)."
}
if ($AdminPassword.Length -lt 8) {
    throw "The test administrator password must contain at least 8 characters."
}
if (-not (Test-Path -LiteralPath $runtimeConfig)) {
    Copy-Item -LiteralPath $exampleConfig -Destination $runtimeConfig
}

$env:TONGTONG_ENVIRONMENT = "test"
$env:TONGTONG_SERVER_PORT = $TestBackendPort
$env:TONGTONG_PUBLIC_WS_URL = "ws://${PublicHost}:${TestBackendPort}/ws"
$env:TONGTONG_ADMIN_USERNAME = $AdminUsername
$env:TONGTONG_ADMIN_PASSWORD = $AdminPassword

Write-Host "Starting isolated test backend: http://${PublicHost}:${TestBackendPort}"
Write-Host "Database: backend/data/test/tongtong-test.db"
Write-Host "Administrator: $AdminUsername"

& python (Join-Path $repoRoot "backend/main.py") --config $runtimeConfig
