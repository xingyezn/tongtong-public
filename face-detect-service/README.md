# face-detect-service

服务器端人脸检测 HTTP 服务（方案一：YOLOv11n-face-v2 INT8 + OpenVINO）。

- 部署模型：`yolo11n-face-v2` INT8 OpenVINO IR（GitHub Release v1.0.0），mAP@0.5≈0.9494，2.48 MB
- 运行时仅依赖 `openvino` + `numpy` + `opencv`（无 PyTorch/Ultralytics），2 线程推理
- 来源/报告：https://github.com/rubythalib-ai/face-detection-openvino-edge

## 目录

- `ov_runtime.py` — 上游推理路径（vendored, MIT）：letterbox → OpenVINO → NMS
- `app.py` — Flask + waitress REST 服务
- `requirements.txt` — Python 依赖
- `deploy/` — systemd unit + 一键安装脚本

## REST API（默认 8090）

- `POST /detect` — 上传图片（`multipart` 字段 `image` 或裸 JPEG/PNG body）
  返回：`{"count", "faces":[{"box":[x1,y1,x2,y2],"confidence"}], "size", "total_ms"}`
- `GET /health` — 服务与模型状态
- `GET /login` / `POST /login` — 后台登录
- `GET /logout` — 退出后台登录
- `GET /api/faces` — 查询人脸库
- `POST /api/faces` — 新增人脸（multipart：`name`、可选 `external_id`/`note`/`image`）
- `GET /api/faces/{id}` — 查询单个人脸
- `PUT/PATCH /api/faces/{id}` — 修改资料；可用 multipart 更新图片，也可用 JSON 更新文字字段
- `DELETE /api/faces/{id}` — 删除人脸
- `POST /api/recognize` — 上传图片进行 1:N 身份识别（multipart `image` 或裸图片）

## 人脸识别接口示例

```bash
# 新增（图片必须只包含一个人脸）
curl -X POST -F "name=张三" -F "external_id=user-001" -F "note=管理员" -F "image=@face.jpg" http://127.0.0.1:8090/api/faces

# 识别
curl -X POST -F "image=@query.jpg" http://127.0.0.1:8090/api/recognize

# 查询 / 修改 / 删除
curl http://127.0.0.1:8090/api/faces
curl -X PATCH -H "Content-Type: application/json" -d '{"name":"新名称","note":"备注"}' http://127.0.0.1:8090/api/faces/1
curl -X DELETE http://127.0.0.1:8090/api/faces/1
```

人脸资料和特征向量保存在 `data/faces.db`，识别阈值默认为 cosine `0.6`，只有分数大于该阈值的结果才会返回，可通过 `FACE_RECOGNITION_THRESHOLD`、`FACE_RECOGNITION_DB`、`FACE_YUNET_MODEL`、`FACE_SFACE_MODEL` 环境变量调整。录入图片必须且只能包含一张人脸，否则接口会返回整改说明且不会写入数据库。后台页面和管理接口需要登录，默认用户名为 `tongtong`、密码为 `tongtong-2026`，可通过 `FACE_ADMIN_USERNAME`、`FACE_ADMIN_PASSWORD`、`FACE_AUTH_SECRET` 环境变量修改；设备使用的 `/detect` 和 `/health` 保持公开。

## 部署（服务器执行）

```bash
cd /opt/face-detect-service
bash deploy/install.sh
```

systemd：`face-detect.service`，端口 8090（百度云安全组需放行）。
