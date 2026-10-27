#!/bin/sh
# VM 에서 한 번: 지표 수집기 서비스 등록 + Prometheus · Grafana 시작
set -e
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)

# Grafana 관리자 비밀번호 — VM 에서 임의로 만들고 git 에 넣지 않는다
[ -f .env ] || { echo "GF_SECURITY_ADMIN_PASSWORD=$(openssl rand -hex 16)" > .env; chmod 600 .env; }

sudo tee /etc/systemd/system/nwlab-exporter.service >/dev/null <<EOF
[Unit]
Description=nwlab metrics exporter (127.0.0.1:9105)
After=docker.service

[Service]
User=$(id -un)
WorkingDirectory=$ROOT/tools
ExecStart=/usr/bin/python3 $ROOT/tools/exporter.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now nwlab-exporter
sudo systemctl restart nwlab-exporter

docker compose up -d
