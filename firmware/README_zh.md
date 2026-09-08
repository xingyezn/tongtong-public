# Tongtong 固件

[English](README.md) | 中文

> **上游项目声明：** 本固件基于开源项目 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 进行二次开发。Tongtong 的硬件适配、自建后端接入、MCP 能力和产品功能均为后续改动；上游项目的著作权声明和许可证继续保留，详见 [LICENSE](LICENSE)。

这是当前 Tongtong 项目的 ESP32 固件。固件通过 WebSocket 连接自建后端，并通过 MCP 提供设备能力；语音识别、模型对话、TTS 和服务器端人脸管理由后端完成。

## 已实现功能

- ESP32-S3 固件启动、Wi-Fi 配网和 WebSocket 长连接。
- 本地 ESP-SR 唤醒词检测。
- Opus 音频编解码、语音上行和 TTS 音频播放。
- VAD 语音段采集、实时打断、待命和唤醒状态切换。
- 屏幕信息读取和待命提示。
- UVC 摄像头当前帧采集，固定使用 480×320 分辨率和 JPEG 上传链路。
- 摄像头 MCP：`self.camera.take_photo`；人脸数量和身份识别统一优先使用后端 `server.face.recognize_current`。本地 ESP-DL 人脸检测代码保留在源码中，便于后续开发，但当前不注册为 MCP 工具。
- 底盘运动 MCP：控制终端完成前进、后退、左转、右转和停止；电机仅作为底层执行部件。
- 云台、舵机、跟随状态和 OTA 等已接入的板级 MCP 能力。

## 主要 MCP 方法

### 摄像头

- `self.camera.take_photo`：重新采集当前帧并上传后端，由后端进行视觉处理。

服务器端人脸录入、识别、删除和修改不在固件中保存数据。后端模型工具会先调用拍照 MCP，再将新图片转发给项目内的 `face-detect-service`。

### 电机

- `self.chassis.go_forward`
- `self.chassis.go_back`
- `self.chassis.turn_left`
- `self.chassis.turn_right`
- `self.chassis.stop`

当前面包板固件的电机控制引脚为 AIN1=GPIO8、AIN2=GPIO9、BIN1=GPIO10、BIN2=GPIO11、STBY=GPIO12。

## 编译和烧录

编译前必须完整阅读 [`docs/BUILD_GUIDE_CN.md`](docs/BUILD_GUIDE_CN.md)。该文件是本项目固件构建的唯一推荐入口，包含：

1. ESP-IDF v5.5.5 环境检查。
2. 分支对应的后端环境选择：`main` 使用生产环境；非 `main` 分支必须由用户确认使用 8081（浩然）或 8082（浩鑫）。
3. OTA/WS 地址配置。
4. ESP-SR/ESP-DL 兼容修复库和 UVC 480×320 补丁的应用。
5. 日常增量编译和无旧缓存的完整编译。
6. 编译成功后自动烧录当前已确认的串口，并进行串口验收。
7. 构建缓存、已验证固件和故障日志的保留规则。

推荐从仓库根目录执行：

```powershell
Get-Content firmware\docs\BUILD_GUIDE_CN.md
.\scripts\build_firmware.ps1 -TestBackendPort 8081 -Flash -Port COM3
```

将端口和后端参数替换为当前实际确认值。不要直接照搬上游 `idf.py` 命令，也不要在未确认串口时执行烧录。编译产物、`firmware\build` 和已验证固件备份不要使用 `git clean` 删除。

## 相关目录

- `main/`：固件源码和板级实现。
- `docs/BUILD_GUIDE_CN.md`：完整构建、烧录和串口调试指南。
- `docs/ESP_SR_ESPDL_CONFLICT_FIX.md`：官方兼容修复说明。
- `../face-detect-service/`：服务器端人脸识别服务源码，由 Git 统一追踪。
