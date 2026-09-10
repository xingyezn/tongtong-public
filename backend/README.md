# Tongtong Backend

[中文](README_zh-CN.md) | English

The backend is an `aiohttp` service that connects Tongtong ESP32 devices to the Qwen Omni Realtime model. It provides the device WebSocket gateway, Opus audio transport, MCP bridging, OTA endpoints, multi-user accounts, device binding, conversation history, permanent memory, administrator controls, and browser-based manual hardware tests.

## Current features

- WebSocket device sessions with wake/listen/abort/standby handling.
- Streaming PCM/Opus uplink and TTS playback with interruption protection.
- Device MCP discovery and tool calls for camera, screen, RGB LED, motor, gimbal, servo, tracking, and OTA capabilities.
- An administrator-managed firmware library with up to 20 `.bin` releases. Admins can upload version metadata and dispatch a selected release to a selected online device. Dispatch is asynchronous: confirm download, validation, and reboot from device logs.
- Per-device model, language, voice, prompt, VAD, memory, and conversation-timeout settings.
- Eight-digit, time-limited device binding codes and per-user device ownership.
- User accounts, administrator accounts, audit records, conversation history, usage/token statistics, and permanent memory.
- Camera upload and latest-frame preview for supervised tests.
- Conversational server-side face tools: register the current frame, recognize the current frame, delete by an ID explicitly provided by the user, and update metadata or replace a sample image.
- The model is not given the face-list tool. Face lists remain available only to the authenticated administrator/manual test page.

## Server-side face recognition

`face_service.py` calls the Git-tracked sibling service at `../face-detect-service/` through `http://127.0.0.1:8090`. Port 8090 is loopback-only and is not a public API; external administration is exposed through the backend's `/admin/face/` proxy. The backend authenticates with the face service login session, then calls `/api/faces` or `/api/recognize`.

Current-frame operations always invoke the firmware's `self.camera.take_photo` first. The JPEG is kept temporarily in backend memory and is never placed in the model prompt. The face service records recognition requests in its recent-request history, including result, count, timing, and a temporary image reference.

Configure the face service under `face_service` in the backend configuration, or from the administrator page. Do not commit real API keys, account passwords, tokens, or deployment configuration.

## Environments and deployment

- `main`: production backend `/opt/tongtong-omni-backend`, service `tongtong-omni`, port `8080`.
- Non-`main`: use an explicitly confirmed test backend. Port `8081` is Hao Ran at `/opt/tongtong-omni-backend-test-adam`, service `tongtong-omni-test-adam`; port `8082` is Hao Xin and must not be overwritten by this workflow.
- The face service runs separately at `/opt/face-detect-service`, service `face-detect.service`, listening only on `127.0.0.1:8090`; do not open 8090 in the cloud security group.

Preserve each server's `config.yaml`, SQLite database, virtual environment, and environment file when uploading application code. Restart only the service belonging to the selected environment. Updating remote `main` requires explicit approval.

## Firmware upload and dispatch

After a successful firmware build, use the administrator page's firmware library to upload `firmware/build/tongtong.bin`, enter a unique version and description, and verify the reported size and SHA-256. The library keeps at most 20 releases. Select an online device to dispatch a release; the page shows command, download, reboot, and reconnect progress. Dispatch is asynchronous and must be confirmed from device logs.

Whether a firmware release is synchronized during a repository release must be explicitly confirmed separately. Never commit administrator credentials, passwords, API keys, device tokens, or deployment secrets. Supply them through server environment variables, ignored local configuration, or the administrator page.

## Local development

From the repository root, create private configuration and start the backend with the project scripts. For a test backend, copy `backend/config.test.example.yaml` to the ignored `backend/config.test.yaml`, select the confirmed test port, and use the isolated test database.

```powershell
python -m py_compile backend\app\*.py
python backend\main.py
```

On Windows PowerShell, if the wildcard is not expanded by Python, use the build/test instructions in the repository documentation or pass the individual files.

## Tests and useful endpoints

```powershell
python backend\tools\test_accounts.py
python backend\tools\test_auth.py
python backend\tools\test_dashboard.py
```

Useful backend endpoints include `/ws`, `/health`, `/ota`, `/ota/activate`, `/api/camera/upload`, `/api/test/tools`, and `/api/test/mcp`. Administrator firmware APIs are `/api/admin/firmware` (list/upload), `DELETE /api/admin/firmware/{id}`, and `POST /api/admin/firmware/{id}/deploy`. Firmware downloads use short-lived private URLs and are stored under `data/firmware`, outside Git. Face-service endpoints are documented in [`../face-detect-service/README.md`](../face-detect-service/README.md).

For the complete repository build, flash, backend environment, and release procedure, see [`../项目使用说明.md`](../项目使用说明.md) and [`../docs/TEST_PRODUCTION_ENVIRONMENTS.zh-CN.md`](../docs/TEST_PRODUCTION_ENVIRONMENTS.zh-CN.md).
