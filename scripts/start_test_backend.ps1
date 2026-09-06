param(
    [string]$PublicHost = "127.0.0.1",
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
$env:TONGTONG_SERVER_PORT = "8082"
$env:TONGTONG_PUBLIC_WS_URL = "ws://${PublicHost}:8082/ws"
$env:TONGTONG_ADMIN_USERNAME = $AdminUsername
$env:TONGTONG_ADMIN_PASSWORD = $AdminPassword

Write-Host "Starting isolated test backend: http://${PublicHost}:8082"
Write-Host "Database: backend/data/test/tongtong-test.db"
Write-Host "Administrator: $AdminUsername"

& python (Join-Path $repoRoot "backend/main.py") --config $runtimeConfig
