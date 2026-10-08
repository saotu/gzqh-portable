#!/usr/bin/env bash
# 卸载 gzqh。若当前在备用线路，先把入口规则切回主线。
set -uo pipefail
SERVICE=ss-failover
SERVICE_FILE=/etc/systemd/system/ss-failover.service
SCRIPT_FILE=/opt/cf_ss_failover.py
STATE_DIR=/opt/ss-failover
LOGROTATE_FILE=/etc/logrotate.d/ss-failover
BIN_FILE=/usr/local/bin/gzqh
[ "${EUID:-$(id -u)}" -eq 0 ] || { echo "请用 root 运行"; exit 1; }
systemctl stop "$SERVICE" 2>/dev/null || true
[ -f "$SCRIPT_FILE" ] && python3 "$SCRIPT_FILE" restore-primary || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "$SERVICE_FILE" "$SERVICE_FILE".bak_* "$SCRIPT_FILE" "$SCRIPT_FILE".bak_* "$LOGROTATE_FILE" "$BIN_FILE"
rm -rf "$STATE_DIR"
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed "$SERVICE" 2>/dev/null || true
echo "已卸载 gzqh（入口端口的 nft 转发规则保留，指向主线）"
