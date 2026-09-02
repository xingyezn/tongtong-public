$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $repo "build\face-tracking-tests"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$exe = Join-Path $outDir "face_tracking_controller_test.exe"

if (-not (Get-Command g++ -ErrorAction SilentlyContinue)) {
    throw "g++ is required to run the host-side controller tests."
}

g++ -std=c++17 -Wall -Wextra -Werror `
    (Join-Path $repo "firmware\main\face_tracking_controller.cc") `
    (Join-Path $repo "tests\face_tracking_controller_test.cc") `
    -I (Join-Path $repo "firmware\main") -o $exe
& $exe
if ($LASTEXITCODE -ne 0) { throw "Face tracking controller tests failed." }
Write-Host "Face tracking controller tests passed."
