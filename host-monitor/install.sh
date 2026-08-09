#!/bin/sh
set -eu

INSTALL_DIR=/opt/onset-host-monitor
SERVICE_FILE=/etc/systemd/system/onset-host-monitor.service
COLLECTOR_SERVICE=/etc/systemd/system/onset-docker-collector.service

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，无法安装"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "缺少 Docker，无法安装"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR/host_monitor.py"
chmod 755 "$INSTALL_DIR/docker_collector.py"
mkdir -p "$INSTALL_DIR/docker-reports"
if [ ! -s "$INSTALL_DIR/agents.env" ]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  printf 'DOCKER_AGENT_SECRET=%s\n' "$SECRET" > "$INSTALL_DIR/agents.env"
fi
chmod 600 "$INSTALL_DIR/agents.env"
if [ -f "$INSTALL_DIR/pve.env" ]; then
  chmod 600 "$INSTALL_DIR/pve.env"
fi
install -m 644 "$INSTALL_DIR/onset-host-monitor.service" "$SERVICE_FILE"
install -m 644 "$INSTALL_DIR/onset-docker-collector.service" "$COLLECTOR_SERVICE"
systemctl daemon-reload
systemctl enable --now onset-host-monitor.service
systemctl enable --now onset-docker-collector.service
echo "小主机监控服务已安装并启动"
