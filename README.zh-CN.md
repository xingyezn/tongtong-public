# 童童（Tongtong）

[English](README.md) | [简体中文](README.zh-CN.md)

> **编译前请先阅读 `firmware/docs/BUILD_GUIDE_CN.md`，并严格按照其中的环境配置、修复库处理、缓存保留、编译和烧录流程执行。**

童童是一套可私有化部署、低延迟的 ESP32 语音助手。本仓库包含 ESP-IDF
固件、对接阿里云百炼 `qwen3.5-omni-plus-realtime` 的 asyncio/aiohttp
服务端，以及用于管理配置和诊断硬件的浏览器控制面板。

## 当前版本特性

- 使用 Qwen3.5-Omni-Realtime 实现端到端流式语音对话。
- 支持多用户注册与登录，账号、设备和数据使用 SQLite 持久化隔离。
- 绑定时只需输入设备端显示的 8 位一次性绑定码，无需填写设备 ID；绑定码有效期为 10 分钟。
- 服务端按账号和来源 IP 双重限制错误尝试次数，并通过数据库原子操作保证一台设备只能绑定一个用户。
- 服务端会自动生成账号内唯一的设备识别码和默认名称，用户可在绑定后修改或解绑设备。
- 每台设备可独立设置模型、语言、音色、人物设定、模型连接复用时长和 VAD 参数。
- 对话记录采用 GPT 风格：从设备离开待命到再次进入待命为一次会话，内部按时间连续保存多轮用户和 AI 消息。
- 用户可软删除已结束的会话；删除内容保留供后台审计，但不再向用户显示，也不会参与模型上下文或长期记忆。
- 支持可控的永久记忆：会话结束后可由 `qwen3.8-max` 保守汇总姓名、偏好等稳定信息；用户可查看、编辑、停用或删除每条信息，并可分别控制每台设备能否使用。
- AI 说话时将生成中的回复文字同步显示在设备屏幕上。
- 用户说话结束后直接快速上传已采集音频，不再进行第二次限速回放，降低响应延迟。
- 服务商保持连接时跨轮复用模型 WebSocket，减少重复握手开销。
- 当前待命周期内按用户和设备保留最近 20 个完整问答；模型连接超时重建时会恢复这些上下文。
- 模型连接复用时长可设置为 1～120 分钟；它不会切分已保存的会话。
- 服务端和设备端使用约 240 ms 启动预缓冲，并采用基于截止时间的发帧节拍，减少断音和卡顿。
- 设备 WebSocket 断开后自动重连，使用指数退避，最长等待 30 秒。
- 支持真正的实时打断：打断时立即停止设备播放，但模型继续生成并保存完整文本；复位待命键单击后打断并继续聆听，双击结束会话。
- 用户明确告别、要求结束对话或让 AI 退下时，AI 完成最后一句回答后会主动让设备进入待命并结束会话。
- 支持千问官方 29 种语音输出语言及自动检测；控制面板采用“中文名称（对应语种文字）”显示。
- 管理面板支持账号内设备、绑定与命名、对话记录、实时日志、每设备模型/VAD 配置、摄像头拍照、OTA 请求和 MCP 硬件测试。

## 系统架构

```text
麦克风 / ESP32 固件
        │  Opus 音频 + JSON 控制消息 + MCP
        ▼
Tongtong aiohttp 服务端
        │  PCM 音频 + Realtime 事件
        ▼
qwen3.5-omni-plus-realtime
        │
        └── 流式文本/音频回复 → 240 ms 缓冲 → 扬声器
```

设备端通过本地 VAD 判断一段讲话何时结束，并将该段语音发送到服务端。服务端负责转发模型、
流式回传生成的语音，并按用户和设备隔离对话状态。一次会话从设备离开待命开始，到用户、AI 或控制面板让设备再次进入待命时结束，内部消息作为
一段连续聊天保存。只有用户启用且设备获准使用的长期记忆才会注入系统提示词。模型 WebSocket 可用时会持续复用；如果仅模型连接重建，
则利用本次会话内的近期语音转写恢复上下文；设备连接重建会形成新的待命边界。AI 回复文字也会随生成过程同步
发送到设备屏幕，不必等整段语音结束。

## 仓库结构

- `firmware/` — 基于 xiaozhi-esp32 的 ESP32 固件。
- `backend/` — aiohttp HTTP/WebSocket 服务、音频桥接、千问集成、MCP 桥接和管理面板。
- `public.yaml` — 可提交的脱敏地址占位配置。
- `private/local.example.yaml` — 私有地址和运行配置模板。
- `scripts/configure_local.py` — 生成被 Git 忽略的运行配置。
- `scripts/build_firmware.ps1` — 配置并构建 ESP32-S3 固件。
- `docs/` — 配置、测试、硬件和安全共享文档。

生成文件、固件产物、密钥、真实部署地址、设备 Token 和本地笔记都不应提交到 Git。

## 支持的对话语言

控制面板提供“自动检测（Auto Detect）”，并包含以下 29 种千问官方语音输出语言：

| 代码 | 语言 | 面板显示 |
| --- | --- | --- |
| `zh` | 中文（普通话） | 中文（普通话） |
| `en` | 英语 | 英语（English） |
| `fr` | 法语 | 法语（Français） |
| `de` | 德语 | 德语（Deutsch） |
| `ru` | 俄语 | 俄语（Русский） |
| `it` | 意大利语 | 意大利语（Italiano） |
| `es` | 西班牙语 | 西班牙语（Español） |
| `pt` | 葡萄牙语 | 葡萄牙语（Português） |
| `ja` | 日语 | 日语（日本語） |
| `ko` | 韩语 | 韩语（한국어） |
| `th` | 泰语 | 泰语（ไทย） |
| `id` | 印度尼西亚语 | 印度尼西亚语（Bahasa Indonesia） |
| `ar` | 阿拉伯语 | 阿拉伯语（العربية） |
| `vi` | 越南语 | 越南语（Tiếng Việt） |
| `tr` | 土耳其语 | 土耳其语（Türkçe） |
| `fi` | 芬兰语 | 芬兰语（Suomi） |
| `pl` | 波兰语 | 波兰语（Polski） |
| `hi` | 印地语 | 印地语（हिन्दी） |
| `nl` | 荷兰语 | 荷兰语（Nederlands） |
| `cs` | 捷克语 | 捷克语（Čeština） |
| `ur` | 乌尔都语 | 乌尔都语（اردو） |
| `fil` | 他加禄语 | 他加禄语（Tagalog） |
| `sv` | 瑞典语 | 瑞典语（Svenska） |
| `da` | 丹麦语 | 丹麦语（Dansk） |
| `he` | 希伯来语 | 希伯来语（עברית） |
| `is` | 冰岛语 | 冰岛语（Íslenska） |
| `ms` | 马来语 | 马来语（Bahasa Melayu） |
| `no` | 挪威语 | 挪威语（Norsk） |
| `fa` | 波斯语 | 波斯语（فارسی） |

所选语言会约束模型回复；辅助语音转写模型接受对应代码时，还会同时设置 ASR 语言提示。
荷兰语、乌尔都语、希伯来语和波斯语使用 ASR 自动识别，但 Omni 模型回复仍会被明确约束为所选语言。

## 本地配置

环境要求：

- Python，并安装 `backend/requirements.txt` 中的依赖。
- 构建固件需要 ESP-IDF 5.4 或更新版本。
- 真实模型回复需要阿里云百炼业务空间和 API Key。

在仓库根目录创建被 Git 忽略的本地配置：

```powershell
Copy-Item private/local.example.yaml private/local.yaml
python -m pip install -r backend/requirements.txt
```

Linux 或 macOS 可使用 `cp private/local.example.yaml private/local.yaml`。编辑
`private/local.yaml`，填写真实 OTA/WS 地址、业务空间 ID，并至少确认以下模型设置：

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

生成运行配置：

```bash
python scripts/configure_local.py
```

该命令会创建：

- `backend/config.yaml`：服务端运行配置。
- `firmware/sdkconfig.defaults.private`：私有 OTA 地址配置。

推荐通过服务进程环境变量注入 `DASHSCOPE_API_KEY`。不要提交 API Key、账号数据库、
设备 Token、真实地址或上述生成文件。完整规则参见
[配置与部署流程](docs/CONFIGURATION_WORKFLOW.md)。

## 启动服务端

```bash
cd backend
python main.py
```

主要路由：

- `GET /health` — 服务健康检查。
- `GET|POST /ota` — 设备启动和 OTA 配置。
- `GET /ws` — 设备音频和控制 WebSocket。
- `GET /` — 需要登录的管理面板。
- `GET|POST /register` 和 `/login` — 用户注册与登录。
- `GET|POST /api/devices/*` — 账号内设备绑定与管理。
- `GET|POST /api/model?device_id=...` — 每台设备的模型、语言、音色、人物设定和模型连接复用设置。
- `GET /api/conversations?device_id=...` — 会话列表；增加 `conversation_id=...` 可读取其中按顺序保存的消息。
- `POST /api/conversations/end|delete` — 结束当前会话或软删除一条已结束的会话。
- `GET|POST /api/memories*` — 查看和管理当前用户的永久记忆。
- `GET|POST /api/features?device_id=...` — 设置每台设备的记忆授权和打断行为。

生产环境应使用 HTTPS 和高强度账号密码；若不对公众开放注册，完成账号创建后应关闭开放注册。

## 构建和烧录固件

编译前必须完整阅读 [`firmware/docs/BUILD_GUIDE_CN.md`](firmware/docs/BUILD_GUIDE_CN.md)。该文件是当前固件编译、烧录和串口验收的唯一推荐入口，包含 ESP-IDF v5.5.5、兼容修复、UVC 480×320、后端选择、增量/全量编译、缓存保留和自动烧录流程。

默认配置面向 bread-compact Wi-Fi ESP32-S3 板型。在已安装 ESP-IDF v5.5.5 的 Windows 上执行：

```powershell
Get-Content firmware\docs\BUILD_GUIDE_CN.md
.\scripts\build_firmware.ps1 -TestBackendPort 8081
```

确认目标串口和后端环境后，构建并烧录设备：

```powershell
.\scripts\build_firmware.ps1 -TestBackendPort 8081 -Flash -Port COM3
```

`main` 分支使用生产后端；非 `main` 分支必须明确选择测试后端：8081 为浩然，8082 为浩鑫。脚本会应用 UVC 和 ESP-SR/ESP-DL 修复，指定 `-Flash` 且编译成功后自动烧录，并保留已验证构建缓存和固件备份。不要直接照搬上游命令，也不要使用 `git clean` 删除这些文件；构建产物仍属于本地文件，不得提交。

## 发布流程

1. 提交当前开发分支的修改，并保留已验证的构建缓存和固件产物。
2. 拉取远程最新 `main`，合并到当前开发分支，解决冲突并完成必要检查。
3. 推送开发分支，汇报目标提交和变更范围。
4. 在修改远程 `main` 前等待用户明确确认。
5. 获得确认后，将远程 `main` 快进到开发分支，并核对两个远程分支指向同一提交。

确认前可以推送开发分支，但不得更新远程 `main`。

## 验证

在仓库根目录运行服务端回归测试：

```bash
python -m py_compile backend/app/account_store.py backend/app/omni_client.py backend/app/session.py backend/app/ws_gateway.py backend/app/dashboard.py
cd backend
python tools/test_accounts.py
python tools/test_auth.py
python tools/test_dashboard.py
python tools/test_omni_pipeline.py
```

测试覆盖账号和设备隔离、绑定、每设备配置、对话持久化、模型工具调用、模型 WebSocket 复用和替换、
多语言设置、240 ms 播放预缓冲、AI 字幕、摄像头上传及 MCP 硬件测试路由。

其他文档：

- [MCP 台架测试](docs/MCP_BENCH_TESTING.md)
- [端到端语音测试](docs/E2E_VOICE_TESTING.md)
- [DRV8833 电机驱动](firmware/docs/MOTOR_DRIVER.md)
- [安全共享检查清单](docs/SHARING.md)

## 安全与共享

本仓库将可共享源码与运行密钥分离。每次推送前请检查
[安全共享清单](docs/SHARING.md)，确认 `public.yaml` 仍只包含脱敏占位地址，并排除构建目录、
录音、固件二进制和生成配置。

固件沿用 `firmware/LICENSE` 中的上游许可证。公开发布服务端前，请先确认希望采用的服务端许可证。
