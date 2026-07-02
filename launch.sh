#!/usr/bin/env bash
set -euo pipefail
TMP_DIR="$(mktemp -d /tmp/gzqh-launch.XXXXXX)"
INSTALL_SCRIPT="$TMP_DIR/install-online.sh"
curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/install-online.sh -o "$INSTALL_SCRIPT"
bash "$INSTALL_SCRIPT"
echo "Using installed gzqh: $(command -v gzqh)"
grep -n '99) 一键卸载\|服务控制（启动/停止/重启/状态）' "$(command -v gzqh)" | sed -n '1,4p' || true
exec gzqh
