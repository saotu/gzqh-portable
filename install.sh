#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE=ss-failover
SERVICE_FILE=/etc/systemd/system/ss-failover.service
SCRIPT_FILE=/opt/cf_ss_failover.py
BIN_FILE=/usr/local/bin/gzqh
LOGROTATE_FILE=/etc/logrotate.d/ss-failover
TMP_BACKUP=""

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

if [ -f "$SERVICE_FILE" ]; then
  TMP_BACKUP="$(mktemp /tmp/ss-failover.service.XXXXXX)"
  cp -a "$SERVICE_FILE" "$TMP_BACKUP"
fi

install -m 0755 "$ROOT_DIR/gzqh" "$BIN_FILE"
install -m 0755 "$ROOT_DIR/cf_ss_failover.py" "$SCRIPT_FILE"
mkdir -p /opt/ss-failover

if [ -n "$TMP_BACKUP" ] && [ -f "$TMP_BACKUP" ]; then
  cp -a "$TMP_BACKUP" "$SERVICE_FILE"
  rm -f "$TMP_BACKUP"
  systemctl daemon-reload || true
  systemctl restart "$SERVICE" || true
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  echo 'Updated existing installation. Existing service parameters were preserved.'
  echo 'Install finished. Run: gzqh'
else
  cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=NFT failover for fixed entry port 10001
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=CHECK_TIMEOUT=0.06
Environment=CHECK_INTERVAL=0.4
Environment=FAIL_THRESHOLD=1
Environment=FORWARD_PORT=10001
Environment=BACKUP_CHECK_INTERVAL=1
Environment=RECOVER_INTERVAL=1
Environment=RECOVER_THRESHOLD=10
Environment=BACKUP_HOST=127.0.0.1
Environment=BACKUP_PORT=10001
Environment=BACKUP_LIST=127.0.0.1:10001
Environment=PRIMARY_STABLE_COUNT=3
Environment=NFT_FAMILY=ip
Environment=NFT_TABLE=nat
Environment=NFT_CHAIN=prerouting
Environment=NFT_POSTROUTING_CHAIN=postrouting
ExecStart=/usr/bin/python3 /opt/cf_ss_failover.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  cat > "$LOGROTATE_FILE" <<'EOF'
/opt/ss-failover/failover.log {
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    create 0644 root root
}
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  echo 'Fresh install completed. Persistent systemd service created and enabled on boot, but not started yet.'
  echo 'Install finished. Run: gzqh'
fi
