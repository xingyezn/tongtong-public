# Tongtong 后端

[English](README.md) | 中文

后端是一个 `aiohttp` 服务，负责连接 Tongtong ESP32 设备和 Qwen Omni Realtime 模型，提供设备 WebSocket 网关、Opus 音频传输、MCP 桥接、OTA 接口、多用户账号、设备绑定、对话记录、长期记忆、管理员控制和浏览器手动测试页面。

## 当前功能

- 设备 WebSocket 会话，以及唤醒、监听、打断、待命状态处理。
- PCM/Opus 音频上下行、流式 TTS 播放和打断保护。
- 自动发现设备 MCP 工具，并支持摄像头、屏幕、RGB 指示灯、电机、云台、舵机、跟随和 OTA 等能力。
- 管理员可维护最多 20 个 `.bin` 固件版本，保存版本号、描述、大小和 SHA-256，并向指定在线终端下发指定版本。下发是异步的，仍需结合设备日志确认下载、校验和重启结果。
- 每台设备独立配置模型、语言、音色、人物设定、VAD、记忆和对话超时。
- 八位限时设备绑定码和按用户隔离的设备所有权。
- 用户/管理员账号、审计记录、对话记录、Token 用量统计和长期记忆。
- 摄像头上传、最新帧预览和受控的硬件手动测试；对话中拍摄的 JPEG 会随对应消息持久化，并在用户仪表板中显示拍摄指令、时间和该轮 AI 描述。
- 对话式服务器人脸功能：录入当前画面、识别当前画面、按用户明确提供的 ID 删除，以及修改资料或替换样本照片。
- 大模型不会获得人脸列表工具；人脸列表只对已认证的管理员页面和手动测试接口开放。

## 服务器端人脸识别

`face_service.py` 调用仓库内追踪的 `../face-detect-service/` 服务，默认地址为 `http://127.0.0.1:8090`。后端先使用人脸服务登录会话，再调用 `/api/faces` 或 `/api/recognize`。

所有当前画面操作都会先调用固件的 `self.camera.take_photo`，再把新 JPEG 转发给人脸服务。最新帧预览仍只暂存在后端内存中；由对话触发的照片会存入账户 SQLite 数据库并关联到对应消息，通过需要登录且校验会话归属的接口读取。图片不会放入模型提示词。人脸服务会将识别请求写入最近请求记录，包含识别结果、人数、耗时和临时图片引用。

人脸服务地址、登录用户名和密码可在后端配置的 `face_service` 节中设置，也可在管理员页面修改。真实 API Key、账号密码、设备 Token 和部署配置不得提交到 Git。

## 环境和部署

- `main`：生产后端 `/opt/tongtong-omni-backend`，服务 `tongtong-omni`，端口 `8080`。
- 非 `main`：必须先确认测试环境。8081 为浩然，目录 `/opt/tongtong-omni-backend-test-adam`，服务 `tongtong-omni-test-adam`；8082 为浩鑫，本流程不得覆盖。
- 人脸服务独立运行在 `/opt/face-detect-service`，端口 `8090`，服务名为 `face-detect.service`。

上传代码时保留服务器原有的 `config.yaml`、SQLite 数据库、虚拟环境和环境变量文件，只重启当前选定环境的服务。更新远程 `main` 必须取得明确同意。

## 固件上传和下发

固件构建成功后，在管理员页面的固件版本库上传 `firmware/build/tongtong.bin`，填写唯一版本号和版本描述，并核对文件大小与 SHA-256。固件库最多保留 20 个版本。选择在线终端即可下发指定版本，页面会显示指令发送、下载、重启和重新上线进度；下发是异步的，最终结果需要结合设备日志确认。

发布流程中是否同步发布固件必须单独取得明确确认。管理员账号、密码、API Key、设备 Token 和部署密钥不得提交到仓库，只能通过服务器环境变量、Git 忽略的本地配置或管理员页面提供。

## 本地开发

在仓库根目录根据项目配置流程创建私有配置并启动后端。测试环境应从 `backend/config.test.example.yaml` 创建被 Git 忽略的 `backend/config.test.yaml`，使用已确认的测试端口和独立测试数据库。

```powershell
python -m py_compile backend\app\*.py
python backend\main.py
```

如果 Windows PowerShell 没有展开 Python 命令中的通配符，请按照项目文档执行，或逐个传入 Python 文件。

## 测试和常用接口

```powershell
python backend\tools\test_accounts.py
python backend\tools\test_auth.py
python backend\tools\test_dashboard.py
```

后端常用接口包括 `/ws`、`/health`、`/ota`、`/ota/activate`、`/api/camera/upload`、`/api/test/tools` 和 `/api/test/mcp`。管理员固件接口包括 `/api/admin/firmware`（列表/上传）、`DELETE /api/admin/firmware/{id}` 和 `POST /api/admin/firmware/{id}/deploy`。固件下载使用短时效随机私有地址，文件保存在 `data/firmware`，不提交到 Git。人脸服务接口见 [`../face-detect-service/README.md`](../face-detect-service/README.md)。

完整的项目构建、烧录、后端环境和发布流程见 [`../项目使用说明.md`](../项目使用说明.md) 以及 [`../docs/TEST_PRODUCTION_ENVIRONMENTS.zh-CN.md`](../docs/TEST_PRODUCTION_ENVIRONMENTS.zh-CN.md)。
