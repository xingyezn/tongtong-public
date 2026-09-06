# Tongtong backend

The backend is an `aiohttp` application providing OTA configuration, a device
WebSocket gateway, Opus conversion, Qwen-Omni integration, MCP bridging, and a
multi-user browser administration page. Users register and log in, bind devices
by entering only the eight-digit, ten-minute code shown on the device, and
manage per-device model/VAD/interruption settings, GPT-style conversation history,
and user-controlled permanent memory summarized by `qwen3.8-max`. Binding attempts
are rate-limited and each device can have only one owner. Account and device data is stored in SQLite under
`backend/data/` by default.

## Isolated test backend

Run development traffic on port `8082` with a separate SQLite database:

```powershell
$env:TONGTONG_ADMIN_PASSWORD = "use-a-strong-local-password"
.\scripts\start_test_backend.ps1 -PublicHost 192.168.1.20
```

The test process reads the ignored `backend/config.test.yaml` (created from
`config.test.example.yaml`) and stores data in
`backend/data/test/tongtong-test.db`. It does not read or write the production
account database. Open `/admin` after signing in with the bootstrap `admin`
account to manage users and devices. For the shared server, run this isolated
service on `8082`; keep production on its own service and database. See
[`docs/TEST_PRODUCTION_ENVIRONMENTS.zh-CN.md`](../docs/TEST_PRODUCTION_ENVIRONMENTS.zh-CN.md)
for the release procedure.

The dashboard lets a user edit every personal-memory field (category, title,
content, and enabled state) and view daily, weekly, monthly, and cumulative
conversation/token usage. The admin page can enable or disable users and
devices, filter devices by owner, and view token totals by user and device.
Token totals are recorded from the realtime provider's reported usage, so
records created before the usage migration have a zero token total.

Configuration is intentionally not tracked. From the repository root, copy
`private/local.example.yaml` to `private/local.yaml`, fill in deployment values,
then run `python scripts/configure_local.py`. This creates the ignored
`backend/config.yaml` expected by `main.py`.

For a production service, inject `DASHSCOPE_API_KEY` using a protected service
environment file. Never place API keys, user passwords, or device tokens in
Git-tracked files.
