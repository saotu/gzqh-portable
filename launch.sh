#!/usr/bin/env bash
set -euo pipefail
TMP_DIR="$(mktemp -d /tmp/gzqh-launch.XXXXXX)"
INSTALL_SCRIPT="$TMP_DIR/install-online.sh"
curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/install-online.sh -o "$INSTALL_SCRIPT"
bash "$INSTALL_SCRIPT"
exec gzqh
