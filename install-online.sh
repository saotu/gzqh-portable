#!/usr/bin/env bash
set -euo pipefail
TMP_DIR="$(mktemp -d /tmp/gzqh-portable.XXXXXX)"
cd "$TMP_DIR"
curl -fsSL -o gzqh-portable.zip https://github.com/yipengnbb/gzqh-portable/releases/latest/download/gzqh-portable.zip
unzip -q -o gzqh-portable.zip
cd gzqh-portable
bash install.sh
printf '\nInstalled. Start with:\n  gzqh\n'
