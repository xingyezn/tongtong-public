#!/usr/bin/env bash
# tongtong-omni-backend 一键部署脚本（Debian 11）
set -e

APP_DIR="/opt/tongtong-omni-backend"
SERVICE_NAME="tongtong-omni"

echo "[1/5] 安装系统依赖 (libopus-dev)"
apt-get update -y
apt-get install -y python3-venv python3-pip libopus-dev git curl

echo "[2/5] 创建应用目录并上传代码"
mkdir -p "$APP_DIR"
# 注意：代码由部署工具（paramiko/sftp）上传到 $APP_DIR，
# 此脚本假定代码已在 $APP_DIR 中。仅做 venv 与依赖安装。
cd "$APP_DIR"

echo "[3/5] 创建 venv 并安装 Python 依赖"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[4/5] 写入 systemd 服务"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Tongtong Omni Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# 百炼 API Key 通过 /etc/tongtong-omni.env 注入，避免写死在配置里
EnvironmentFile=-/etc/tongtong-omni.env

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] 启动服务"
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}
systemctl status ${SERVICE_NAME} --no-pager || true

echo "完成！服务状态见上。日志：journalctl -u ${SERVICE_NAME} -f"
