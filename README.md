# Tongtong

[English](README.md) | [简体中文](README.zh-CN.md)

> **Before compiling, read `firmware/docs/BUILD_GUIDE_CN.md` and follow its environment, patch, cache, build, and flashing instructions.**

Tongtong is a self-hosted, low-latency ESP32 voice assistant. This repository
contains the ESP-IDF firmware, an asyncio/aiohttp backend for Alibaba Cloud
Model Studio's `qwen3.5-omni-plus-realtime`, and a browser-based administration
and hardware diagnostics dashboard.

## Highlights

- End-to-end streaming voice conversations with Qwen3.5-Omni-Realtime.
- Multi-user registration and login with SQLite-backed account isolation.
- Secure device binding by entering only the on-device eight-digit, ten-minute
  one-time code. The server generates an account-local identifier and default
  name, which the user can customize later.
- Binding attempts are rate-limited by both account and source IP, and an atomic
  database claim guarantees that one device can belong to only one user.
- Independent model, language, voice, persona, model-connection reuse, and VAD
  settings for every bound device.
- GPT-style conversation history: one saved conversation contains an ordered
  stream of user and assistant messages from leaving standby until returning to it.
- Users can soft-delete completed conversations. Deleted content remains available
  for administrative audit but is hidden from users, model context, and memory jobs.
- Opt-in permanent user memory. Completed conversations can be conservatively
  summarized by `qwen3.8-max`; users can view, edit, disable, or delete every
  fact and independently allow memory use on each device.
- Streaming assistant captions on the device display as the AI speaks.
- Lower turn latency by uploading captured audio without a second paced replay.
- Persistent model WebSocket reuse between turns whenever the provider keeps the
  connection open.
- Per-device continuity for the latest 20 complete turns in the current standby
  cycle, including context recovery after a model WebSocket reconnect.
- Configurable model-connection reuse from 1 to 120 minutes; this does not split
  the persisted conversation.
- A 240 ms startup jitter buffer on both the server and device, plus deadline-based
  frame pacing to reduce broken or stuttering speech.
- Automatic device WebSocket reconnection with exponential backoff up to 30 seconds.
- True barge-in stops device playback immediately while model generation continues
  and its complete text is retained. The
  standby button interrupts and resumes listening on one click, or ends the
  conversation on a double click; wake-word interruption and all three controls
  are configurable per device.
- When a user clearly says goodbye, asks to end the conversation, or dismisses the
  assistant, the device enters standby automatically after the final reply finishes.
- 29 officially supported speech-output languages, plus automatic language
  detection. The dashboard displays every option as a Chinese name followed by
  its native name.
- Browser dashboard for account-owned devices, conversation history, logs,
  per-device model/language/voice/persona/VAD settings, camera capture, OTA
  requests, and MCP hardware tests.

## Architecture

```text
Microphone / ESP32 firmware
        │  Opus audio + JSON control + MCP
        ▼
Tongtong aiohttp backend
        │  PCM audio + Realtime events
        ▼
qwen3.5-omni-plus-realtime
        │
        └── streaming text/audio response → 240 ms buffer → speaker
```

The device performs local VAD and sends one captured utterance to the backend.
The backend forwards it to the model, streams the generated audio back, and keeps
conversation state by user and device. One conversation begins when the device
leaves standby and ends only when it returns to standby; its ordered messages remain one chat record.
Enabled long-term memories are injected as a delimited part of the system prompt
only for devices whose owner allowed it. Model-side WebSocket state is reused when
possible; recent turns from the active standby cycle restore context after a provider
reconnect. A device reconnect creates a new standby boundary. Assistant transcript deltas are also forwarded to
the device display while speech is playing.

## Repository layout

- `firmware/` — ESP32 firmware based on xiaozhi-esp32.
- `backend/` — aiohttp HTTP/WebSocket server, audio bridge, Qwen integration,
  MCP bridge, and dashboard.
- `public.yaml` — tracked, redacted endpoint placeholders only.
- `private/local.example.yaml` — template for private endpoints and runtime
  settings.
- `scripts/configure_local.py` — generates ignored runtime configuration.
- `scripts/build_firmware.ps1` — configures and builds the ESP32-S3 firmware.
- `docs/` — configuration, testing, hardware, and sharing documentation.

Generated files, compiled firmware, credentials, deployment hosts and endpoints,
device tokens, and local notes must not be committed.

## Supported conversation languages

The dashboard provides `Auto Detect` plus the following 29 official
Qwen3.5-Omni speech-output languages:

| Code | Language | Dashboard label |
| --- | --- | --- |
| `zh` | Chinese (Mandarin) | 中文（普通话） |
| `en` | English | 英语（English） |
| `fr` | French | 法语（Français） |
| `de` | German | 德语（Deutsch） |
| `ru` | Russian | 俄语（Русский） |
| `it` | Italian | 意大利语（Italiano） |
| `es` | Spanish | 西班牙语（Español） |
| `pt` | Portuguese | 葡萄牙语（Português） |
| `ja` | Japanese | 日语（日本語） |
| `ko` | Korean | 韩语（한국어） |
| `th` | Thai | 泰语（ไทย） |
| `id` | Indonesian | 印度尼西亚语（Bahasa Indonesia） |
| `ar` | Arabic | 阿拉伯语（العربية） |
| `vi` | Vietnamese | 越南语（Tiếng Việt） |
| `tr` | Turkish | 土耳其语（Türkçe） |
| `fi` | Finnish | 芬兰语（Suomi） |
| `pl` | Polish | 波兰语（Polski） |
| `hi` | Hindi | 印地语（हिन्दी） |
| `nl` | Dutch | 荷兰语（Nederlands） |
| `cs` | Czech | 捷克语（Čeština） |
| `ur` | Urdu | 乌尔都语（اردو） |
| `fil` | Tagalog | 他加禄语（Tagalog） |
| `sv` | Swedish | 瑞典语（Svenska） |
| `da` | Danish | 丹麦语（Dansk） |
| `he` | Hebrew | 希伯来语（עברית） |
| `is` | Icelandic | 冰岛语（Íslenska） |
| `ms` | Malay | 马来语（Bahasa Melayu） |
| `no` | Norwegian | 挪威语（Norsk） |
| `fa` | Persian | 波斯语（فارسی） |

The selected language constrains the model response and, where accepted by the
auxiliary transcription model, also supplies an ASR language hint. Dutch, Urdu,
Hebrew, and Persian use transcription auto-detection while the Omni response is
still explicitly constrained to the selected language.

## Local configuration

Requirements:

- Python with the packages from `backend/requirements.txt`.
- ESP-IDF 5.4 or newer for firmware builds.
- A Model Studio workspace and API key for real model responses.

Create the ignored local configuration from the repository root:

```bash
cp private/local.example.yaml private/local.yaml
python -m pip install -r backend/requirements.txt
```

On PowerShell, use `Copy-Item private/local.example.yaml private/local.yaml` for
the first command. Edit `private/local.yaml` and set real deployment endpoints,
the workspace ID, and at least these model settings:

```yaml
backend:
  dashscope:
    model: "qwen3.5-omni-plus-realtime"
    language: "zh"
    conversation_timeout_minutes: 10
  memory:
    enabled: true
    model: "qwen3.8-max"
```

Generate the ignored runtime files:

```bash
python scripts/configure_local.py
```

This creates:

- `backend/config.yaml` for the backend.
- `firmware/sdkconfig.defaults.private` for the private OTA endpoint.

Prefer injecting `DASHSCOPE_API_KEY` through the backend service environment.
Never commit an API key, account database, device token, real endpoint, or the
generated runtime files. See
[Configuration and deployment workflow](docs/CONFIGURATION_WORKFLOW.md).

## Run the backend

```bash
cd backend
python main.py
```

Default routes include:

- `GET /health` — service health.
- `GET|POST /ota` — device bootstrap and OTA configuration.
- `GET /ws` — device audio/control WebSocket.
- `GET /` — authenticated administration dashboard.
- `GET|POST /register` and `/login` — account registration and login.
- `GET|POST /api/devices/*` — account-owned device binding and management.
- `GET|POST /api/model?device_id=...` — per-device model, language, voice,
  persona, and model-connection-reuse settings.
- `GET /api/conversations?device_id=...` — conversation list; add
  `conversation_id=...` to read its ordered messages.
- `POST /api/conversations/end|delete` — end the active conversation or soft-delete
  a completed conversation.
- `GET|POST /api/memories*` — view and manage the signed-in user's permanent memory.
- `GET|POST /api/features?device_id=...` — per-device memory and interruption controls.

Use HTTPS in production, use strong account passwords, and disable open
registration after onboarding if the service is not intended for public signup.

## Build and flash firmware

Before building, read [`firmware/docs/BUILD_GUIDE_CN.md`](firmware/docs/BUILD_GUIDE_CN.md) completely. It is the single source of truth for the current firmware build and flash workflow, including IDF v5.5.5, compatibility fixes, UVC 480x320 configuration, backend selection, incremental/full builds, cache retention, serial validation, and automatic flashing.

The default configuration targets the bread-compact Wi-Fi ESP32-S3 board. On
Windows with ESP-IDF 5.5.5 installed:

```powershell
Get-Content firmware\docs\BUILD_GUIDE_CN.md
.\scripts\build_firmware.ps1 -TestBackendPort 8081
```

Build and flash a connected device after confirming the target port and backend:

```powershell
.\scripts\build_firmware.ps1 -TestBackendPort 8081 -Flash -Port COM3
```

For `main`, use the production backend. For non-`main` branches, the test
backend port must be explicitly selected: 8081 is Hao Ran and 8082 is Hao Xin.
The script applies the tracked UVC and ESP-SR/ESP-DL fixes, automatically flashes
after a successful build when `-Flash` is supplied, and preserves verified build
caches and firmware backups. Do not use upstream commands or `git clean` to
remove those files. Build outputs remain local artifacts and must not be committed.

## Verification

Run the backend regression checks from the repository root:

```bash
python -m py_compile backend/app/account_store.py backend/app/omni_client.py backend/app/session.py backend/app/ws_gateway.py backend/app/dashboard.py
cd backend
python tools/test_accounts.py
python tools/test_auth.py
python tools/test_dashboard.py
python tools/test_omni_pipeline.py
```

The tests cover account and device isolation, binding, per-device settings,
conversation persistence, model tool calls, persistent/replacement WebSockets,
multilingual settings, the 240 ms playback buffer, assistant captions, camera
upload, and direct MCP hardware-test routing.

Useful additional documentation:

- [MCP bench testing](docs/MCP_BENCH_TESTING.md)
- [End-to-end voice testing](docs/E2E_VOICE_TESTING.md)
- [DRV8833 motor driver](firmware/docs/MOTOR_DRIVER.md)
- [Safe sharing checklist](docs/SHARING.md)

## Security and sharing

This repository is designed to keep source code separate from runtime secrets.
Before every push, review [Safe sharing checklist](docs/SHARING.md), verify that
`public.yaml` still contains only redacted example endpoints, and exclude build
directories, recordings, firmware binaries, and generated configuration.

The firmware retains its upstream license in `firmware/LICENSE`. Confirm the
license you want for the backend before publishing it as open source.
