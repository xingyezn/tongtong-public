param(
    [switch]$Flash,
    [string]$Port = "",
    [switch]$Clean,
    [ValidateSet("8081", "8082")]
    [string]$TestBackendPort = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$firmware = Join-Path $repo "firmware"

Set-Location $repo

$branch = (git branch --show-current).Trim()
if ([string]::IsNullOrWhiteSpace($branch)) {
    throw "Cannot determine the current Git branch; do not build from detached HEAD."
}
if ($branch -ne "main" -and [string]::IsNullOrWhiteSpace($TestBackendPort)) {
    throw "Non-main branch requires an explicit -TestBackendPort 8081 (HaoRan) or 8082 (HaoXin)."
}
if ($branch -eq "main" -and -not [string]::IsNullOrWhiteSpace($TestBackendPort)) {
    throw "-TestBackendPort is only for non-main branches; main uses the production configuration."
}
if (-not [string]::IsNullOrWhiteSpace($TestBackendPort)) {
    Write-Output "Selected non-main test backend: port $TestBackendPort"
}

python scripts\configure_local.py

if ($branch -ne "main") {
    $backendConfig = Get-Content -LiteralPath (Join-Path $repo "backend\config.yaml") -Raw
    $serverPort = [regex]::Match($backendConfig, '(?m)^\s+port:\s*(\d+)\s*$').Groups[1].Value
    if ($serverPort -ne $TestBackendPort) {
        throw "Selected test port $TestBackendPort does not match backend/config.yaml server.port=$serverPort. Update private/local.yaml only after confirming the owner."
    }
    $firmwareDefaults = Get-Content -LiteralPath (Join-Path $firmware "sdkconfig.defaults.private") -Raw
    if ($firmwareDefaults -notmatch (':'+[regex]::Escape($TestBackendPort)+'/(ota|$)')) {
        throw "Selected test port $TestBackendPort does not match firmware/sdkconfig.defaults.private OTA URL. Confirm the backend owner before building."
    }
}

Set-Location $firmware

$env:IDF_TOOLS_PATH = "C:\Espressif"
$idfPython = "C:\Espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe"
$idfActivate = "C:\Espressif\frameworks\esp-idf-v5.5.5\tools\activate.py"
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$exp = (& $idfPython $idfActivate --export 2>$null | Select-Object -Last 1).Trim()
$ErrorActionPreference = $previousErrorActionPreference
if ([string]::IsNullOrWhiteSpace($exp)) {
    throw "Failed to export ESP-IDF v5.5.5 environment."
}
. $exp

$defaults = "sdkconfig.defaults;sdkconfig.defaults.private"
if ($Clean) {
    if (Test-Path -LiteralPath "sdkconfig") {
        Remove-Item -LiteralPath "sdkconfig"
    }
    idf.py fullclean
}

# A fresh clone has neither sdkconfig nor managed_components. Generate the
# target configuration first; reconfigure then lets Component Manager restore
# the managed dependencies before the project patches are applied.
if (-not (Test-Path -LiteralPath "sdkconfig")) {
    idf.py -D "SDKCONFIG_DEFAULTS=$defaults" set-target esp32s3
}
idf.py reconfigure

# esp_video 1.3.1 allocates three maximum-size UVC frames by default. On the
# N16R8 this competes with ESP-SR and ESP-DL for the shared 8 MB PSRAM. Apply
# our tracked one-frame patch after Component Manager has restored dependencies.
$uvcDriver = Join-Path $firmware "managed_components\espressif__esp_video\src\device\esp_video_usb_uvc_device.c"
$uvcResolutionPatch = Join-Path $repo "patches\esp_video_uvc_480x320.patch"
if (-not (Select-String -Path $uvcDriver -SimpleMatch "#define UVC_DEVICE_FRAME_COUNT          1" -Quiet)) {
    $uvcContent = Get-Content -LiteralPath $uvcDriver -Raw
    $uvcDefault = "#define UVC_DEVICE_FRAME_COUNT          3"
    $uvcReplacement = @"
// The ESP32-S3 N16R8 shares 8 MB PSRAM between UVC, ESP-SR and ESP-DL.
// One buffer is sufficient for the still-photo / serial face-tracking path:
// every frame is consumed and returned before the next one is requested.
#define UVC_DEVICE_FRAME_COUNT          1
"@
    if (-not $uvcContent.Contains($uvcDefault)) {
        throw "Cannot apply the ESP Video UVC memory patch; inspect $uvcDriver."
    }
    $uvcContent = $uvcContent.Replace($uvcDefault, $uvcReplacement.TrimEnd())
    [System.IO.File]::WriteAllText($uvcDriver, $uvcContent, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "Applied UVC single-buffer memory patch."
}

# Keep the negotiated USB-UVC stream at the tested 480x320 mode. This is
# required for the ESP-DL face detector and must be reapplied after a fresh
# Component Manager restore because managed_components is intentionally ignored.
if (-not (Select-String -Path $uvcDriver -SimpleMatch "#define UVC_DEVICE_FRAME_WIDTH          480" -Quiet)) {
    if (Select-String -Path $uvcDriver -SimpleMatch "#define UVC_DEVICE_FRAME_WIDTH          320" -Quiet) {
        $uvcContent = Get-Content -LiteralPath $uvcDriver -Raw
        $uvcContent = $uvcContent.Replace("#define UVC_DEVICE_FRAME_WIDTH          320", "#define UVC_DEVICE_FRAME_WIDTH          480")
        $uvcContent = $uvcContent.Replace("#define UVC_DEVICE_FRAME_HEIGHT         240", "#define UVC_DEVICE_FRAME_HEIGHT         320")
        [System.IO.File]::WriteAllText($uvcDriver, $uvcContent, (New-Object System.Text.UTF8Encoding($false)))
        Write-Output "Restored UVC resolution from 320x240 to 480x320."
    } else {
    git -C $repo apply --check $uvcResolutionPatch
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot apply the ESP Video UVC 480x320 resolution patch; inspect $uvcDriver."
    }
    git -C $repo apply $uvcResolutionPatch
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply the ESP Video UVC 480x320 resolution patch."
    }
    Write-Output "Applied UVC 480x320 resolution patch."
    }
}

# ESP-SR and ESP-DL on ESP32-S3 currently share conflicting conv2d symbols in
# the registry libdl_lib.a. Use the tested replacement from the repository.
$espSrLib = Join-Path $firmware "managed_components\espressif__esp-sr\lib\esp32s3\libdl_lib.a"
$espSrFix = Join-Path $repo "firmware\vendor\esp-sr-libdl-fix\libdl_lib.a"
if (-not (Test-Path -LiteralPath $espSrFix)) {
    throw "ESP-SR/ESP-DL compatibility library is missing: $espSrFix"
}
if (-not (Test-Path -LiteralPath $espSrLib)) {
    throw "ESP-SR library was not restored by Component Manager: $espSrLib"
}
Copy-Item -LiteralPath $espSrFix -Destination $espSrLib -Force
Write-Output "Applied ESP-SR/ESP-DL compatibility library: $espSrFix"

# Reconfigure again so the patched managed sources and replacement library
# are reflected in the final build graph.
idf.py reconfigure
idf.py build

if ($Flash) {
    if ([string]::IsNullOrWhiteSpace($Port)) {
        idf.py flash
    } else {
        idf.py -p $Port flash
    }
}
