# esp-sr / esp-dl 符号冲突修复说明

## 现象

在同一 ESP32-S3 固件里同时链入 `esp-sr`（唤醒词 multinet/mn7）与 `esp-dl`
（human_face_detect）后，**唤醒词 detect 失效**（Wake feed 正常但永不触发），
或反过来 **人脸检测框异常**（大量假框）。此前曾误判为 SRAM 余量不足。

## 根因（官方确认）

Espressif esp-dl issue
[espressif/esp-dl#302](https://github.com/espressif/esp-dl/issues/302)：

> esp32-s3 上 esp-sr 与 esp-dl 的 **conv2d 汇编实现存在部分函数名冲突**，
> 链接器不报错但符号被串用，导致两者之一的计算错乱。

官方临时修复：用修正版 `libdl_lib.a` 替换 esp-sr 组件里的同名文件。

## 本仓库的处理（已验证）

- 修正版库已存放在：`vendor/esp-sr-libdl-fix/libdl_lib.a`
- 应用方式（build 前执行；组件在 gitignored 的 managed 目录里，clean 后需重放）：
  ```powershell
  copy firmware\vendor\esp-sr-libdl-fix\libdl_lib.a `
       firmware\managed_components\espressif__esp-sr\lib\esp32s3\libdl_lib.a
  ```
- 备份：原版 `libdl_lib.a` 来自 esp-sr registry 组件，删除 managed 目录重新
  拉取即可还原，无需在仓库内保存。
- 验证结果：替换后同固件内
  - `EspdlProbe`：灰图 0 框、内置人脸照 1 框（score≈0.90）；
  - 唤醒自检（注入 TTS “ni hao tong tong”）触发 `DETECTED`。

## 正式修复建议

替换 `.a` 是过渡方案。升级到含官方修复的 esp-sr 版本（本地 2.2.0 过旧，
官方近期线 2.4.x 已含修复）是正式做法，建议放到官方 IDF6 新基座迁移时实施，
届时同步验证唤醒词 assets 与 API 兼容。

## 相关诊断代码

- `boards/common/camera_face_detect_local.{h,cc}`：本地 ESP-DL 人脸检测工具；
- `boards/common/espdl_link_probe.cc`：灰图/内置人脸诊断（boot 由
  `FACE_WAKE_SELFTEST_ON_BOOT` 宏开启，默认关）；
- `audio_service::RunWakeWordSelfTest`：唤醒词注入自检（同一宏控制，默认关）。
