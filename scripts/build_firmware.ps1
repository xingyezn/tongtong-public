param(
    [switch]$Flash,
    [string]$Port = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$firmware = Join-Path $repo "firmware"

Set-Location $repo
python scripts\configure_local.py

Set-Location $firmware
if (Test-Path sdkconfig) {
    Remove-Item -LiteralPath sdkconfig
}

$defaults = "sdkconfig.defaults;sdkconfig.defaults.private"
. "C:\Espressif\esp-idf\export.ps1"
idf.py -D "SDKCONFIG_DEFAULTS=$defaults" set-target esp32s3

# esp_video 1.3.1 allocates three maximum-size UVC frames by default. On the
# N16R8 this competes with ESP-SR and ESP-DL for the shared 8 MB PSRAM. Apply
# our tracked one-frame patch after Component Manager has restored dependencies.
$uvcDriver = Join-Path $firmware "managed_components\espressif__esp_video\src\device\esp_video_usb_uvc_device.c"
$uvcPatch = Join-Path $repo "patches\esp_video_uvc_single_buffer.patch"
if (-not (Select-String -Path $uvcDriver -SimpleMatch "#define UVC_DEVICE_FRAME_COUNT          1" -Quiet)) {
    git -C $repo apply --check $uvcPatch
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot apply the ESP Video UVC memory patch; inspect $uvcDriver."
    }
    git -C $repo apply $uvcPatch
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply the ESP Video UVC memory patch."
    }
}

idf.py -D "SDKCONFIG_DEFAULTS=$defaults" build

if ($Flash) {
    if ([string]::IsNullOrWhiteSpace($Port)) {
        idf.py flash
    } else {
        idf.py -p $Port flash
    }
}
