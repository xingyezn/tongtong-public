# Tongtong 固件编译指南（当前分支）

适用对象：本机二次开发当前固件。固件基于 xiaozhi-esp32 v2.1.0，当前默认分支为
`test/main-face-isolation`（对照线）。包含本地人脸检测（ESP-DL）与远程 8090 人脸
检测双模式、USB-UVC 摄像头、自建后端心跳与拍照测试。

> 说明：本仓库是脱敏共享仓库。真实 OTA/WS 地址与密钥一律不写进公共文件，见
> `docs/CONFIGURATION_WORKFLOW.md`。

---

## 1. 环境要求（Windows）

- ESP-IDF **v5.5.5**：`C:\Espressif\frameworks\esp-idf-v5.5.5`
- 工具链：`C:\Espressif\tools\`
- Python venv：`C:\Espressif\python_env\idf5.5_py3.11_env`
- 目标芯片：**ESP32-S3 N16R8**（16MB Flash + 8MB PSRAM），面包板 `bread-compact-wifi` +
  USB UVC 摄像头
- 首次拉取组件需网络（可走本机代理 127.0.0.1:7897）

每次新开终端先加载 IDF 环境：

```powershell
$env:IDF_TOOLS_PATH = "C:\Espressif"
# 用实际 venv 里的 python 生成环境导出脚本并 source
$exp = (& "C:\Espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe" `
        "C:\Espressif\frameworks\esp-idf-v5.5.5\tools\activate.py" --export 2>$null |
        Select-Object -Last 1).Trim()
. $exp
```

---

## 2. 检查代码分支

```powershell
cd D:\AdamData\esp32\tongtong-public
git branch --show-current   # 应为 test/main-face-isolation
git log --oneline -3
```

服务器人脸识别（远程 8090 定稿）打在 tag `v0.1.0-face-server`；最新开发在分支
`test/main-face-isolation`。

---

## 3. 私有配置（真实 OTA/WS 地址）

1. 编辑 `private\local.yaml`（git 忽略，仓库只保留 `local.example.yaml` 模板），填真实值：
   ```yaml
   firmware:
     ota_url: http://<服务器>:8080/ota        # 生产；测试后端用 8081
   backend:
     server:
       public_ws_url: ws://<服务器>:8080/ws
   ```
2. 生成运行时配置（会写 `firmware/sdkconfig.defaults.private` 与 `backend/config.yaml`）：
   ```powershell
   cd D:\AdamData\esp32\tongtong-public
   python scripts\configure_local.py
   ```
   确认生成结果：
   ```powershell
   type firmware\sdkconfig.defaults.private   # 应含 CONFIG_OTA_URL="http://..."
   ```

---

## 4. esp-sr / esp-dl 符号冲突修复（**每次 clean 后必做**）

本地人脸检测与唤醒词同固件依赖一个官方补丁：用修正版 `libdl_lib.a` 覆盖 esp-sr
组件里的同名库（详见 `firmware/docs/ESP_SR_ESPDL_CONFLICT_FIX.md`）。

```powershell
copy /Y firmware\vendor\esp-sr-libdl-fix\libdl_lib.a ^
         firmware\managed_components\espressif__esp-sr\lib\esp32s3\libdl_lib.a
```

> `managed_components` 是 git 忽略的下载目录；删除后重新拉取会还原成未修复的库，
> 必须重放本步，否则会复现“链入 esp-dl 后唤醒词失效”。

---

## 5. 编译

首次 / 换芯片 / 改了 `sdkconfig.defaults*` 时，**删除 sdkconfig** 后按 defaults 全量生成：

```powershell
cd D:\AdamData\esp32\tongtong-public\firmware
if (Test-Path sdkconfig) { Remove-Item sdkconfig }
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.private" set-target esp32s3
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.private" build
```

日常增量编译（sdkconfig 已存在）：

```powershell
idf.py build
```

产物：`build\tongtong.bin`（含本地人脸检测模型，固件约 4.6MB；esp-sr 组件与
摄像头相关组件在首次构建时自动从 registry 下载）。

> 新增/删除 `boards/common/*.cc` 后若链接报 “undefined reference”，先
> `idf.py reconfigure` 让 GLOB 重新扫描再 `idf.py build`。

---

## 6. 烧录

当前分区表为 `partitions/v2/16m_face_test.csv`（双 OTA 各 6MB，assets 在 0xC20000），
人脸检测模型内嵌于 app（rodata），无需单独烧模型分区。

```powershell
cd D:\AdamData\esp32\tongtong-public\firmware
idf.py -p COM3 flash monitor
```

等价完整命令：

```powershell
python -m esptool --chip esp32s3 -p COM3 -b 460800 --before default_reset --after hard_reset write_flash `
  --flash_mode dio --flash_size 16MB --flash_freq 80m `
  0x0      build\bootloader\bootloader.bin `
  0x8000   build\partition_table\partition-table.bin `
  0xd000   build\ota_data_initial.bin `
  0x20000  build\tongtong.bin `
  0xc20000 build\generated_assets.bin
```

> 注意 assets 地址与主分支 `16m.csv` 布局（0x800000）不同，勿混用。

---

## 7. 编译期测试/诊断开关（默认关）

- `FACE_WAKE_SELFTEST_ON_BOOT`：开机跑一次灰图+内置人脸照检测，并在 20s 后注入
  TTS “你好童童”做唤醒自检（打印 `EspdlProbe: gray/photo`、`wake self-test: HIT/MISS`）。
  临时开启：在 `firmware/main/CMakeLists.txt` 追加并重编：
  ```cmake
  target_compile_definitions(${COMPONENT_LIB} PRIVATE FACE_WAKE_SELFTEST_ON_BOOT=1)
  ```
- `SERVER_DETECT_BENCH_ON_BOOT`：开机延迟跑 50 次远程人脸检测基准（`RunServerDetectBench`）。

验证完请把这些临时宏移除/置默认关再发布。

---

## 8. 验证要点

1. 串口应看到：
   ```
   MCP: Add tool: self.camera.face_detect      # 仅本地 ESP-DL
   CustomWakeWord: Command: ni hao tong tong ... Action: wake
   ```
2. 对麦克风说“你好童童”应触发 `Custom wake word detected`（需板子旁真人声）。
3. 后端面板：
   - 设备心跳后应显示**在线**（后端收到 `/heartbeat`）；
   - “摄像头手动测试”的“人脸检测”下拉可切 **线上(8090) / 本地(ESP-DL)**，拍照后
     返回 JSON 顶部 `mode` 即所用模式，照片会画检测框显示。
4. 唤醒/本地人脸共存的深层问题与修复见 `firmware/docs/ESP_SR_ESPDL_CONFLICT_FIX.md`。

---

## 9. 串口读取与调试

### 9.1 找串口号

Windows 在“设备管理器 → 端口(COM 和 LPT)”查看 CH343/串口对应的 COMx；或命令行：

```powershell
# 用 IDF venv 里的 python 列出可用串口
& "C:\Espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe" -m serial.tools.list_ports
```

示例输出 `COM3`。注意：USB 转串口(调试)与本机 USB OTG 摄像头是两路，烧录/日志用 CH343
串口，不要占用摄像头那条。

### 9.2 打开串口日志

```powershell
cd D:\AdamData\esp32\tongtong-public\firmware
idf.py -p COM3 monitor            # 只读日志
idf.py -p COM3 flash monitor      # 烧录后直接看启动日志
```

常用操作（esp-idf-monitor）：
- `Ctrl+]` 退出 monitor；
- `Ctrl+T` 后 `Ctrl+H` 查看帮助；`Ctrl+T` 后 `Ctrl+R` 复位设备；
- `Ctrl+T` 后 `Ctrl+L`/输入过滤词可过滤输出；
- 串口被 monitor 独占，**不要同时开两个 monitor/esptool**。

把日志存盘（便于回传分析）：

```powershell
idf.py -p COM3 monitor 2>&1 | Out-File monitor.log -Encoding utf8
```

### 9.3 日志级别与关键 TAG

monitor 默认显示 INFO 及以上；`ESP_LOGD` 的调试信息默认不显示。查看某 TAG 的 DEBUG 日志，
可先临时在代码里设：

```cpp
esp_log_level_set("AudioService", ESP_LOG_DEBUG);   // 例：开 AudioService 调试
```

或改 `sdkconfig.defaults`/menuconfig 的 `CONFIG_LOG_DEFAULT_LEVEL` 为 DEBUG 后重编（量产固件请保持 INFO）。

本固件排查用到的关键 TAG：

| TAG | 说明 | 级别 |
|---|---|---|
| `CustomWakeWord` | 唤醒词初始化/命中（`Custom wake word detected`） | INFO |
| `AudioService` | 唤醒喂音/音频任务 | INFO/DEBUG |
| `FaceDetectLocal` | **本地 ESP-DL 人脸检测**结果 | INFO |
| `ServerDetect` | **远程 8090 人脸检测**结果/上传耗时 | INFO |
| `EspdlProbe` / `wake self-test` | 自检（需开 `FACE_WAKE_SELFTEST_ON_BOOT`） | INFO |
| `Heartbeat` | 后端心跳（每 15s，正常为 DEBUG `status=200`；错误为 WARN） | DEBUG |
| `Esp32Camera` / `ServerDetect bench` | 相机流、远程基准 | INFO |

心跳每 15s 打一次 DEBUG，若要在 monitor 里确认“心跳正在上报”，临时把 `Heartbeat` TAG 的
`ESP_LOGD` 改为 `ESP_LOGI`，或整体日志级别调 DEBUG 后重编一次。

### 9.4 判断思路

1. 设备不上线/后端看不到 → 看 `Network connected` 后是否有 `Heartbeat`；再对后端面板确认端口。
2. 唤醒词不触发 → 看 `CustomWakeWord: Command` 是否存在、自检 `wake self-test: HIT/MISS`。
3. 拍照测试不返回 → 看设备 `CameraTest: camera test done mode=...` 与后端面板返回 `mode`。
4. 崩溃/看门狗 → 搜索 `Guru Meditation` / `panic` / `wdt`，连同 backtrace 一起留存分析。

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| 编译报错找不到组件/卡下载 | 网络问题；配置代理后重跑，或删 `managed_components` + `firmware/dependencies.lock` 重新 resolve |
| 链入 esp-dl 后唤醒失效 | 未执行第 4 步 lib 替换，重放补丁 |
| `undefined reference to CameraFaceDetectLocalJson` | 新增文件没进 GLOB，`idf.py reconfigure` 后重编 |
| sdkconfig 有 BOM/中文 | 用纯文本/UTF-8 无 BOM 编辑，或用 `idf.py menuconfig` |
| 上传/面板看不到设备 | 确认跑过心跳（固件连的后端端口与你面板一致） |
