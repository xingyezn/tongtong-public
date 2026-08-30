# Tongtong

Tongtong is a self-hosted ESP32 voice assistant. The repository contains the
ESP-IDF firmware, an asyncio backend for an Omni voice model, and an embedded
web monitoring page.

## Repository layout

- `firmware/` — ESP32 firmware based on xiaozhi-esp32.
- `backend/` — aiohttp WebSocket/HTTP server, audio bridge, MCP bridge, and monitor.
- `private/local.example.yaml` — the single template for deployment-specific settings.
- `scripts/configure_local.py` — writes the ignored backend runtime config and firmware OTA override.

Generated files, compiled firmware, credentials, deployment hosts, device tokens,
and local tutorials are intentionally not tracked.

## Configure a local deployment

1. Copy `private/local.example.yaml` to `private/local.yaml`.
2. Fill in the OTA URL, public WebSocket URL, dashboard password, and any service settings.
3. Install the backend requirements, then generate runtime configuration:

   ```bash
   python -m pip install -r backend/requirements.txt
   python scripts/configure_local.py
   ```

`private/local.yaml` and `backend/config.yaml` are ignored by Git. Use
`DASHSCOPE_API_KEY` through the service environment rather than committing an API key.

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
cd firmware
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.private" set-target esp32s3
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.private" build
```

The default configuration targets the bread-compact Wi-Fi board. Build outputs
are ignored and must not be committed.

## Sharing policy

This repository is intended to be safe to push to a shared remote. Before each
push, run the secret scan documented in [docs/SHARING.md](docs/SHARING.md).

The firmware retains its upstream license at `firmware/LICENSE`. Confirm the
license you want for the backend before publishing it as open source.
