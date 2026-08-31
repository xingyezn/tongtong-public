# 人脸检测软件测试

在摄像头和舵机到位前，可使用静态图片验证人脸检测结果和跟随控制输入。测试工具输出统一的结果结构：图片尺寸、人脸框数量、每个人脸框以及归一化中心点 `center_x/center_y`。后续接入 ESP-DL 时保持该结构不变，仅替换检测器实现。

## 运行

```bash
python -m pip install -r scripts/requirements-face-test.txt
python scripts/face_detection_test.py path/to/face.jpg \
  --output build/face-detection.json \
  --annotated-dir build/face-annotated
```

默认使用 OpenCV Haar 检测器作为主机端 smoke test，不代表最终 ESP32-S3 推理精度。固件版本使用 ESP-DL Human Face Detection，验证重点是输入格式、边界框、中心点和无脸/多人脸策略。

## 验收要点

- 单人脸：`face_count >= 1`，中心点与图片实际位置一致；
- 无人脸：`face_count == 0`，跟随控制应保持停止或回中；
- 多人脸：按最大人脸框选择目标；
- 所有中心点均在 `[0, 1]`，可直接转换为云台误差；
- 测试图片、输出结果和标注图放在本地 `build/`，不要提交到共享仓库。

## ESP32-S3 板端真实推理

固件开启 `CONFIG_FACE_DETECTION_TEST_ON_BOOT` 后，会在启动阶段解码内置 JPEG，并调用 ESP-DL `HumanFaceDetect` 完成一次真实推理。模型使用官方 `human_face_det` 独立 Flash 分区，刷写时由组件自动写入 `0xE00000`。

```powershell
cd firmware
. C:\Espressif\esp-idf\export.ps1
idf.py -p COM3 build flash monitor
```

串口中关注 `FaceDetectTest` 日志，例如：

```text
Inference complete: 4 face(s), 140 ms
face score=0.9883 box=(0,78)-(53,144)
```

耗时是本次 `run()` 调用及后处理时间；首次启动还会包含模型加载和内存映射日志。接入摄像头后应保持 RGB888 输入和该日志格式，便于比较帧率。
