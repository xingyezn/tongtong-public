# Tongtong

Tongtong is a self-hosted ESP32 voice assistant. The repository contains the
ESP-IDF firmware, an asyncio backend for an Omni voice model, and an embedded
web monitoring page.

## Repository layout

- `firmware/` — ESP32 firmware based on xiaozhi-esp32.
- `backend/` — aiohttp WebSocket/HTTP server, audio bridge, MCP bridge, and monitor.
- `public.yaml` — tracked redacted endpoint placeholders; it contains no deployment data.
- `private/local.example.yaml` — template for real endpoints, credentials, passwords, and tokens.
- `scripts/configure_local.py` — merges the public-safe defaults and ignored local settings, then writes ignored runtime files.

Generated files, compiled firmware, credentials, deployment hosts and endpoints, device tokens,
and local tutorials are intentionally not tracked.

## Configure a local deployment

1. Keep the redacted endpoint placeholders in `public.yaml` unchanged.
2. Copy `private/local.example.yaml` to `private/local.yaml`.
3. Fill in the real OTA/WS endpoints, dashboard password, credentials, and any local service settings.
4. Install the backend requirements, then generate runtime configuration:

   ```bash
   python -m pip install -r backend/requirements.txt
   python scripts/configure_local.py
   ```

`private/local.yaml` and `backend/config.yaml` are ignored by Git. `public.yaml`
is tracked but must retain its redacted examples—never real endpoints, credentials,
or passwords. Use
`DASHSCOPE_API_KEY` through the service environment rather than committing an API key.

For the complete local-build, production-deployment, and public-artifact policy,
read [docs/CONFIGURATION_WORKFLOW.md](docs/CONFIGURATION_WORKFLOW.md).

## Run the backend

```bash
cd backend
python main.py
```

The backend serves health, OTA, device WebSocket, and monitoring endpoints. Do
not expose the service until you set a strong dashboard password and enable an
appropriate device authentication strategy.

## Build firmware

Install ESP-IDF 5.4 or newer, configure the private settings above, then:

```bash
powershell -ExecutionPolicy Bypass -File scripts/build_firmware.ps1
```

The script regenerates the ignored private defaults and configures ESP-IDF with
them. Firmware configuration rejects `your-server.example`, so a build cannot
silently produce an image with the public placeholder OTA endpoint. Add
`-Flash -Port COM3` when the board is connected.

For the ESP32-S3 USB camera configuration, use this script rather than calling
`idf.py build` directly: it applies the tracked low-memory UVC patch required
to share the N16R8's 8 MB PSRAM with ESP-SR and ESP-DL.

The default configuration targets the bread-compact Wi-Fi board. Build outputs
are ignored and must not be committed.

For the DRV8833 dual-motor wiring, safety limits, and MCP chassis commands,
read [firmware/docs/MOTOR_DRIVER.md](firmware/docs/MOTOR_DRIVER.md).
For deterministic network tests that bypass microphone and model input, read
[docs/MCP_BENCH_TESTING.md](docs/MCP_BENCH_TESTING.md).
For automated voice-to-model-to-MCP regression tests, read
[docs/E2E_VOICE_TESTING.md](docs/E2E_VOICE_TESTING.md).
For the planned USB camera and SG90 face-tracking feature, read
[docs/FACE_TRACKING_PLAN.md](docs/FACE_TRACKING_PLAN.md).
For the planned 1.28-inch GC9A01 round TFT, read
[docs/ROUND_TFT_PLAN.md](docs/ROUND_TFT_PLAN.md).

## Sharing policy

This repository is intended to be safe to push to a shared remote. Before each
push, follow [docs/CONFIGURATION_WORKFLOW.md](docs/CONFIGURATION_WORKFLOW.md)
and run the secret scan documented in [docs/SHARING.md](docs/SHARING.md).

The firmware retains its upstream license at `firmware/LICENSE`. Confirm the
license you want for the backend before publishing it as open source.
