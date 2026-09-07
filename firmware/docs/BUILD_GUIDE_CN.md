# Tongtong 固件构建指南

本指南是本仓库二次开发固件的唯一推荐构建入口。目标是：在一台新电脑上下载任意开发分支后，按本文完成配置、补丁应用、编译、烧录和串口验证。

> 编译前必须先阅读本文。不要直接照搬上游 `xiaozhi-esp32` 的构建命令；本项目需要额外的 UVC 内存补丁和 ESP-SR/ESP-DL 兼容库。

## 1. 目录和分支

以下命令均从仓库根目录执行，`<仓库目录>` 替换为实际路径：

```powershell
Set-Location <仓库目录>
git branch --show-current
git status --short
```

固件工程目录是 `<仓库目录>\firmware`，自动构建脚本是 `<仓库目录>\scripts\build_firmware.ps1`。脚本会自动生成本地配置、加载 IDF、恢复依赖并应用构建补丁。

不要把一个工作树的 `firmware\build` 目录复制到另一台电脑或另一工作树。ESP-IDF 构建目录记录了绝对路径；新电脑应使用自己的构建目录。

## 2. 安装环境

- Windows
- ESP-IDF **v5.5.5**
- ESP32-S3 N16R8：16 MB Flash、8 MB PSRAM
- Python：由 ESP-IDF 安装器创建的 IDF v5.5 Python 环境
- 首次解析 managed components 需要网络

推荐安装位置如下；如果电脑上的路径不同，修改 `scripts\build_firmware.ps1` 中的路径变量：

```text
C:\Espressif\frameworks\esp-idf-v5.5.5
C:\Espressif\python_env\idf5.5_py3.11_env
C:\Espressif\tools
```

确认 IDF：

```powershell
idf.py --version
```

应显示 ESP-IDF v5.5.5。脚本会通过 `activate.py` 自动加载环境，因此不要求每次手工执行旧版 `export.ps1`。

## 3. 配置本地后端和固件地址

公共仓库不保存真实 URL、密钥或设备配置。首次构建前执行：

```powershell
Set-Location <仓库目录>
Copy-Item private\local.example.yaml private\local.yaml
notepad private\local.yaml
python scripts\configure_local.py
```

在 `private\local.yaml` 填入当前环境地址：

- `main` 分支使用生产后端 `http://<服务器>:8080/ota`；
- 非 `main` 分支必须由用户明确选择测试后端：8081 为浩然，8082 为浩鑫；
- WebSocket 地址也必须与所选环境一致。

远程后端部署目录和服务也必须按端口匹配：

| 环境 | 端口 | 部署目录 | systemd 服务 |
| --- | ---: | --- | --- |
| 生产 | 8080 | `/opt/tongtong-omni-backend` | `tongtong-omni` |
| 浩然测试 | 8081 | `/opt/tongtong-omni-backend-test-adam` | `tongtong-omni-test-adam` |
| 浩鑫测试 | 8082 | `/opt/tongtong-omni-backend-test` | `tongtong-omni-test` |

部署时不能只根据“测试环境”字样选择目录，必须先确认使用浩然还是浩鑫。

构建命令中的 `-TestBackendPort` 不是装饰参数：脚本会同时校验生成的
`backend\config.yaml` 服务端口和 `firmware\sdkconfig.defaults.private` OTA 地址。端口未确认、或配置与选择不一致时，构建会直接停止。

生成的 `firmware\sdkconfig.defaults.private` 和 `backend\config.yaml` 被 Git 忽略，不要提交。确认 OTA 配置：

```powershell
Get-Content firmware\sdkconfig.defaults.private
```

如果只修改了 OTA 地址，建议使用第 5 节的 `-Clean` 重新生成 `sdkconfig`，避免旧配置被复用。

## 4. 项目必须的构建修复

构建脚本会在每次构建前自动处理以下内容：

1. `esp_video` 默认申请 3 个 UVC 帧缓冲；在 ESP32-S3 N16R8 上会与 ESP-SR、ESP-DL 争用 PSRAM。脚本将其调整为 1 个缓冲，适合当前拍照和人脸跟踪链路。
2. ESP-SR 与 ESP-DL 在 ESP32-S3 上存在 conv2d 符号兼容问题。脚本用仓库内已验证的官方修复库覆盖 managed component 中的 `libdl_lib.a`。

修复源文件分别位于：

```text
patches\esp_video_uvc_single_buffer.patch
firmware\vendor\esp-sr-libdl-fix\libdl_lib.a
```

`managed_components` 是依赖解析产生的目录，执行 `fullclean` 或重新解析组件后可能恢复默认内容；必须通过构建脚本重新应用修复。详见 [ESP_SR_ESPDL_CONFLICT_FIX.md](ESP_SR_ESPDL_CONFLICT_FIX.md)。

## 5. 推荐构建方式

### 5.1 日常增量构建

```powershell
Set-Location <仓库目录>
# 先确认本分支对应的测试后端：8081=浩然，8082=浩鑫
powershell -ExecutionPolicy Bypass -File .\scripts\build_firmware.ps1 -TestBackendPort 8081
```

脚本会执行 `configure_local.py`、依赖修复、`idf.py reconfigure` 和 `idf.py build`。产物在 `firmware\build`。

### 5.2 新电脑或验证无旧缓存的全量构建

```powershell
Set-Location <仓库目录>
powershell -ExecutionPolicy Bypass -File .\scripts\build_firmware.ps1 -Clean -TestBackendPort 8081
```

该模式会删除当前工作树的 `firmware\sdkconfig` 并执行 `idf.py fullclean`，随后按两个 defaults 文件重新配置目标芯片。它不会删除仓库内的补丁、修复库或其它工作树的缓存。

### 5.3 编译成功后自动烧录

先确认设备管理器中的目标串口，再执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_firmware.ps1 -Flash -Port COM3 -TestBackendPort 8081
```

把 `COM3` 换成实际串口，把 `8081` 换成用户确认的后端端口。构建成功后脚本会自动烧录并复位设备；不要对不确定的串口执行烧录。非 `main` 分支不填写 `-TestBackendPort` 时脚本会拒绝构建。

## 6. 不推荐但可用于排障的手工方式

只有构建脚本无法运行时才使用手工命令。先从仓库根目录生成配置，再加载 IDF：

```powershell
python scripts\configure_local.py
$env:IDF_TOOLS_PATH = "C:\Espressif"
$exp = (& "C:\Espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe" `
  "C:\Espressif\frameworks\esp-idf-v5.5.5\tools\activate.py" --export 2>$null |
  Select-Object -Last 1).Trim()
. $exp
Set-Location firmware
idf.py -D "SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.private" set-target esp32s3
idf.py reconfigure
idf.py build
```

手工方式仍必须先确保第 4 节的两个修复已由脚本执行过，否则不能认为生成的是正确固件。

## 7. 固件烧录地址和产物

不要手写旧版本的 `0xC20000` assets 地址，也不要假设人脸模型内嵌在 app。当前分区和模型/资源地址可能随分支变化，烧录时以当前构建生成的 `firmware\build\flash_args` 为准：

```powershell
Get-Content firmware\build\flash_args
```

推荐始终使用 `build_firmware.ps1 -Flash -Port <COMx>`，由 ESP-IDF 使用当前构建产物和地址完成烧录。常见产物包括：

```text
firmware\build\tongtong.bin
firmware\build\bootloader\bootloader.bin
firmware\build\partition_table\partition-table.bin
firmware\build\flash_args
```

不要提交 `firmware\build`、`sdkconfig`、生成的配置、真实固件或日志。
`firmware\dependencies.lock` 是例外：它用于锁定组件版本，必须保留并提交。

## 8. 串口调试和验收

列出串口：

```powershell
& "C:\Espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe" -m serial.tools.list_ports
```

查看日志：

```powershell
Set-Location <仓库目录>\firmware
idf.py -p COM3 monitor
```

按 `Ctrl+]` 退出 monitor。串口由 monitor 独占，不要同时打开多个 monitor 或烧录进程。

基础验收应看到：

- Wi-Fi/网络连接成功；
- 唤醒词模块正常加载并能命中唤醒词；
- 设备周期性向配置的后端发送心跳；
- 摄像头和人脸检测链路没有持续复位；
- 人脸检测由后端手动测试触发，不执行固件开机内置人脸自检。

出现 `Guru Meditation`、`StoreProhibited`、`watchdog` 或反复重启时，保存完整串口日志以及 `build\flash_args`，不要先删除构建目录。

## 9. 构建缓存和已验证固件

构建缓存与固件备份应保留，便于回退和定位环境问题。清理前先复制或确认备份存在；不要用 `git clean -fdx` 删除它们。

本机已有的历史验证备份不属于新电脑的必需输入，其他电脑不应依赖这些绝对路径：

```text
firmware\build-copied-good-20260907
firmware\release\known-good-20260907
```

新电脑的正确性以 `-Clean` 能否从仓库文件、组件下载和本机 IDF 环境独立构建为准，而不是以复制旧 `build` 目录为准。

## 10. 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 `idf.py` | 安装/加载 IDF v5.5.5，或直接运行构建脚本 |
| 组件下载失败 | 检查网络和代理后重跑；不要提交 managed components |
| 唤醒词或人脸检测异常 | 确认使用构建脚本，脚本会重放 UVC 单帧和 `libdl_lib.a` 修复 |
| OTA 地址不对 | 修改 `private\local.yaml`，重新运行 `configure_local.py`，必要时使用 `-Clean` |
| 烧录地址不确定 | 查看当前 `firmware\build\flash_args`，不要使用历史固定地址 |
| 新增源码后链接不到 | 先运行构建脚本；脚本包含 `idf.py reconfigure` |
| 设备反复复位 | 保存串口完整日志和构建信息，保留 `firmware\build` 后再排查 PSRAM/组件版本 |

相关配置和部署说明见 [docs/CONFIGURATION_WORKFLOW.md](../../docs/CONFIGURATION_WORKFLOW.md)。
