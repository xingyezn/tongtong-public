# Tongtong Firmware

This directory contains the current Tongtong ESP32 firmware. The device connects to the self-hosted backend over WebSocket and exposes device capabilities through MCP. Speech recognition, model conversation, TTS, and server-side face management are handled by the backend.

## Implemented features

- ESP32-S3 startup, Wi-Fi provisioning, and persistent WebSocket connection.
- Local ESP-SR wake-word detection.
- Opus audio codec, voice uplink, and TTS playback.
- VAD speech capture, interruption, standby, and wake states.
- Screen information, brightness, and theme controls.
- RGB indicator control and state reporting; default color is R=0, G=0, B=255.
- UVC camera capture at a fixed 480x320 JPEG pipeline.
- Camera MCP tools: `self.camera.take_photo` and local `self.camera.face_detect_local`.
- Motor MCP controls: forward, backward, turns, spin, stop, and direct GPIO test.
- Connected gimbal, servo, tracking-state, OTA, and other board MCP capabilities.

The normal conversational face registration and recognition flow is server-side. The backend captures a fresh frame through the camera MCP and forwards it to the Git-tracked `face-detect-service`.

## Build and flash

Before compiling, read [`docs/BUILD_GUIDE_CN.md`](docs/BUILD_GUIDE_CN.md) completely. It is the single recommended build entry for this fork and covers IDF v5.5.5, branch-specific backend selection, OTA/WS configuration, ESP-SR/ESP-DL compatibility fixes, the UVC 480x320 patch, incremental/full builds, automatic flashing, serial validation, and cache retention.

From the repository root:

```powershell
Get-Content firmware\docs\BUILD_GUIDE_CN.md
.\scripts\build_firmware.ps1 -TestBackendPort 8081 -Flash -Port COM3
```

Use the user-confirmed backend port and the actual target serial port. Do not copy upstream build commands, flash an unconfirmed port, or delete the verified build cache and firmware backups with `git clean`.

## Important paths

- `main/`: firmware source and board implementations.
- `docs/BUILD_GUIDE_CN.md`: complete build, flash, and serial-debug guide.
- `docs/ESP_SR_ESPDL_CONFLICT_FIX.md`: compatibility-fix notes.
- `../face-detect-service/`: server-side face service tracked in this repository.
