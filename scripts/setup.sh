#!/usr/bin/env bash
# Linux setup (Debian / Ubuntu / Raspberry Pi OS).
#
#   ./scripts/setup.sh                   create venv, install packages, create .env
#   ./scripts/setup.sh --service         ...and install + (re)start the systemd service
#   ./scripts/setup.sh --remove-service  stop and remove the systemd service
#
# Safe to re-run after `git pull`: it updates packages and restarts the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="heartbeat-publisher"
UNIT="/etc/systemd/system/${SERVICE}.service"
cd "$APP_DIR"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

if [ "${1:-}" = "--remove-service" ]; then
  $SUDO systemctl disable --now "$SERVICE" 2>/dev/null || true
  $SUDO rm -f "$UNIT"
  $SUDO systemctl daemon-reload
  echo "[setup] Service removed."
  exit 0
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[setup] python3 not found. Install it with: sudo apt install -y python3 python3-venv"
  exit 1
fi

if [ ! -x venv/bin/python ]; then
  echo "[setup] Creating virtual environment..."
  if ! "$PYTHON" -m venv venv; then
    rm -rf venv
    echo "[setup] venv creation failed. On Debian/Ubuntu run: sudo apt install -y python3-venv"
    exit 1
  fi
fi

echo "[setup] Installing packages..."
venv/bin/python -m pip install --quiet --upgrade pip
venv/bin/python -m pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "[setup] Created .env from .env.example -> edit it now: nano $APP_DIR/.env"
fi

echo
venv/bin/python publisher.py --show-id
echo

if [ "${1:-}" = "--service" ]; then
  if ! grep -Eq '^HIVEMQ_HOST=[^[:space:]]+' .env; then
    echo "[setup] .env has no HIVEMQ_HOST yet. Fill in .env, then re-run with --service."
    exit 1
  fi
  RUN_USER="${SUDO_USER:-$(id -un)}"
  echo "[setup] Installing systemd service '$SERVICE' (runs as $RUN_USER)..."
  $SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=MQTT heartbeat publisher
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart="${APP_DIR}/venv/bin/python" "${APP_DIR}/publisher.py"
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE" >/dev/null
  $SUDO systemctl restart "$SERVICE"
  sleep 2
  $SUDO systemctl --no-pager --lines=5 status "$SERVICE" || true
  echo
  echo "[setup] Live logs: journalctl -u $SERVICE -f"
else
  echo "[setup] Done. Test with:  venv/bin/python publisher.py --once"
  echo "[setup] Run at boot:      ./scripts/setup.sh --service"
fi
