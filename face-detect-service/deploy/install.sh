#!/usr/bin/env bash
# face-detect-service one-shot install (Debian 11 / Python 3.9)
# Run as root on the deployment host, from the repo root.
set -euo pipefail

APP_DIR=/opt/face-detect-service
MODEL_URL=https://github.com/rubythalib-ai/face-detection-openvino-edge/releases/download/v1.0.0/yolo11n-face-v2_openvino.tar.gz
RECOG_DIR=$APP_DIR/model/recognition

mkdir -p "$APP_DIR"
if [ "$(pwd)" != "$APP_DIR" ]; then
  echo "==> Copy source to $APP_DIR"
  cp -r . "$APP_DIR" || true
  cd "$APP_DIR"
else
  echo "==> Already in $APP_DIR, running in place"
fi

echo "==> Python venv"
python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt

echo "==> Download + extract OpenVINO INT8 model"
mkdir -p model
if [ ! -d model/int8_openvino_model ]; then
  curl -fsSL -o /tmp/yolo11n-face-v2_openvino.tar.gz "$MODEL_URL"
  tar xzf /tmp/yolo11n-face-v2_openvino.tar.gz -C model
fi
ls model/

echo "==> Download face recognition models"
mkdir -p "$RECOG_DIR" "$APP_DIR/data"
if [ ! -s "$RECOG_DIR/face_detection_yunet_2023mar.onnx" ]; then
  curl -fsSL -o "$RECOG_DIR/face_detection_yunet_2023mar.onnx" \
    https://files.kde.org/digikam/facesengine/yunet/face_detection_yunet_2023mar.onnx
fi
if [ ! -s "$RECOG_DIR/face_recognition_sface_2021dec.onnx" ]; then
  curl -fsSL -o "$RECOG_DIR/face_recognition_sface_2021dec.onnx" \
    https://mirrors.bfsu.edu.cn/kde-application/digikam/facesengine/dnnface/face_recognition_sface_2021dec.onnx
fi

echo "==> Install systemd unit"
cp deploy/face-detect.service /etc/systemd/system/face-detect.service
systemctl daemon-reload
systemctl enable --now face-detect
systemctl --no-pager status face-detect --lines=10 || true

echo "==> Done. Smoke test:"
curl -fsS http://127.0.0.1:8090/health || true
