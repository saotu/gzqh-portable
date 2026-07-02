#!/usr/bin/env bash
set -euo pipefail
SERVICE=ss-failover
SERVICE_FILE=/etc/systemd/system/ss-failover.service
SCRIPT_FILE=/opt/cf_ss_failover.py
STATE_DIR=/opt/ss-failover
LOGROTATE_FILE=/etc/logrotate.d/ss-failover
BIN_FILE=/usr/local/bin/gzqh

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "$SERVICE_FILE"
rm -f "$SCRIPT_FILE"
rm -f "$LOGROTATE_FILE"
rm -f "$BIN_FILE"
rm -rf "$STATE_DIR"
systemctl daemon-reload || true
systemctl reset-failed "$SERVICE" 2>/dev/null || true

echo "Uninstalled gzqh / ss-failover components"
