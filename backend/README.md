# Tongtong backend

The backend is an `aiohttp` application providing OTA configuration, a device
WebSocket gateway, Opus conversion, Qwen-Omni integration, MCP bridging, and a
multi-user browser administration page. Users register and log in, bind devices
by entering only the eight-digit, ten-minute code shown on the device, and
manage per-device model/VAD/interruption settings, GPT-style conversation history,
and user-controlled permanent memory summarized by `qwen3.8-max`. Binding attempts
are rate-limited and each device can have only one owner. Account and device data is stored in SQLite under
`backend/data/` by default.

Configuration is intentionally not tracked. From the repository root, copy
`private/local.example.yaml` to `private/local.yaml`, fill in deployment values,
then run `python scripts/configure_local.py`. This creates the ignored
`backend/config.yaml` expected by `main.py`.

For a production service, inject `DASHSCOPE_API_KEY` using a protected service
environment file. Never place API keys, user passwords, or device tokens in
Git-tracked files.
