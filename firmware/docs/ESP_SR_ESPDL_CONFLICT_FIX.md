# ESP-SR / ESP-DL 构建兼容修复

## 背景

ESP32-S3 固件同时使用 ESP-SR 唤醒词和 ESP-DL 人脸检测时，需要两项项目级修复：

1. ESP-SR 与 ESP-DL 的 conv2d 汇编实现存在符号兼容问题；
2. USB-UVC 默认申请三个大帧缓冲，会和 ESP-SR、ESP-DL 争用 N16R8 的 8 MB PSRAM。

缺少任意一项，都可能表现为唤醒词异常、人脸检测异常、模型加载失败，或进入待命后崩溃重启。

## 修复来源

- ESP-SR/ESP-DL 兼容库：firmware/vendor/esp-sr-libdl-fix/libdl_lib.a
- UVC 单帧补丁：patches/esp_video_uvc_single_buffer.patch

libdl_lib.a 是仓库内已验证的官方修复版本。UVC 补丁将
UVC_DEVICE_FRAME_COUNT 从 3 调整为 1，当前拍照和人脸跟踪链路不需要同时保留三帧。

## 如何应用

不要手工长期修改 managed_components。使用仓库根目录的构建脚本：

powershell -ExecutionPolicy Bypass -File .\scripts\build_firmware.ps1

脚本在 idf.py reconfigure 后、正式编译前自动：

- 修改 managed esp_video 源码为单帧缓冲；
- 用仓库内修复库覆盖 managed esp-sr 的 libdl_lib.a；
- 执行 idf.py build。

使用 -Clean 时依赖会被重新恢复，脚本会再次应用这两项修复：

powershell -ExecutionPolicy Bypass -File .\scripts\build_firmware.ps1 -Clean

因此新电脑不需要复制旧的 build 目录，也不需要手工下载或提交 managed components。

## 验证

构建后可检查 UVC 生成源码：

Select-String -Path firmware\managed_components\espressif__esp_video\src\device\esp_video_usb_uvc_device.c -Pattern "UVC_DEVICE_FRAME_COUNT"

应显示值为 1。编译和烧录后，通过串口确认唤醒词模型正常加载，设备进入待命后不持续复位；人脸检测通过后端手动测试触发。当前固件不包含开机内置人脸、纯色图片或唤醒词自检。

## 故障排查

如果再次出现唤醒词失效、模型分配失败或待命重启：

1. 保存完整串口日志；
2. 保留 firmware\build 和 firmware\build\flash_args；
3. 重新运行构建脚本的 -Clean 模式；
4. 确认构建输出包含 Applied UVC single-buffer memory patch 和 Applied ESP-SR/ESP-DL compatibility library 提示。

不要先执行 git clean -fdx，以免删除可用于对比的构建输出。
