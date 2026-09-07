# face-detect-service

服务器端人脸识别 HTTP 服务（YuNet + SFace）。

- 运行时仅依赖 `openvino` + `numpy` + `opencv`（无 PyTorch/Ultralytics），2 线程推理
- 来源/报告：https://github.com/rubythalib-ai/face-detection-openvino-edge

## 目录

- `recognition.py` — YuNet 人脸检测、SFace 特征提取和人脸库匹配
- `app.py` — Flask + waitress REST 服务
- `requirements.txt` — Python 依赖
- `deploy/` — systemd unit + 一键安装脚本

## REST API（默认 8090）

- `POST /api/recognize` — 上传图片并返回检测到的人脸数量、已识别数量、身份、框和置信度
  返回：`{"count", "detected_count", "recognized_count", "faces":[{"box":[x,y,w,h],"id","name","score"}], "threshold"}`
- `GET /health` — 服务与模型状态
- `GET /login` / `POST /login` — 后台登录
- `GET /logout` — 退出后台登录
- `GET /api/faces` — 查询人脸库
- `POST /api/faces` — 新增人脸（multipart：`name`、可选 `external_id`/`note`/`image`）
- `GET /api/faces/{id}` — 查询单个人脸
- `PUT/PATCH /api/faces/{id}` — 修改资料；可用 multipart 更新图片，也可用 JSON 更新文字字段
- `DELETE /api/faces/{id}` — 删除人脸

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

人脸资料和特征向量保存在 `data/faces.db`，识别阈值默认为 cosine `0.6`，可通过 `FACE_RECOGNITION_THRESHOLD`、`FACE_RECOGNITION_DB`、`FACE_YUNET_MODEL`、`FACE_SFACE_MODEL` 环境变量调整。后台页面和管理接口需要登录，必须通过 `FACE_ADMIN_USERNAME`、`FACE_ADMIN_PASSWORD` 和 `FACE_AUTH_SECRET` 环境变量配置，不在仓库中保存默认账号、密码或密钥；公开接口仅保留 `/health`，人脸处理统一使用已认证的 `/api/recognize`。

## 部署（服务器执行）

```bash
cd /opt/face-detect-service
bash deploy/install.sh
```

systemd：`face-detect.service`，端口 8090（百度云安全组需放行）。
