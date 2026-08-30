# Tongtong backend

The backend is an `aiohttp` application providing OTA configuration, a device
WebSocket gateway, Opus conversion, Qwen-Omni integration, MCP bridging, and a
browser monitoring page.

Configuration is intentionally not tracked. From the repository root, copy
`private/local.example.yaml` to `private/local.yaml`, fill in deployment values,
then run `python scripts/configure_local.py`. This creates the ignored
`backend/config.yaml` expected by `main.py`.

For a production service, inject `DASHSCOPE_API_KEY` using a protected service
environment file. Never place API keys, server passwords, or device tokens in
Git-tracked files.
