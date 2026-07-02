#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE=/etc/systemd/system/ss-failover.service
SCRIPT_FILE=/opt/cf_ss_failover.py
BIN_FILE=/usr/local/bin/gzqh
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
  systemctl daemon-reload || true
  systemctl restart ss-failover || true
  rm -f "$TMP_BACKUP"
  cat <<'EOF'
Updated existing installation.
Program files were replaced, existing service parameters were preserved.

Start with:
  gzqh

Uninstall with:
  bash uninstall.sh
EOF
else
  cat <<'EOF'
Installed program files.

No existing service config was found.
Start with:
  gzqh
Then run install/repair from the menu to generate initial service config.

Uninstall with:
  bash uninstall.sh
EOF
fi
