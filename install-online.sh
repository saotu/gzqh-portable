#!/usr/bin/env bash
set -euo pipefail
TMP_DIR="$(mktemp -d /tmp/gzqh-portable.XXXXXX)"
cd "$TMP_DIR"
curl -fsSL -o gzqh-portable.zip https://github.com/saotu/gzqh-portable/releases/latest/download/gzqh-portable.zip
unzip -q -o gzqh-portable.zip
cd gzqh-portable
bash install.sh
if [ -t 0 ] && [ -t 1 ]; then
  exec gzqh
else
  echo 'Install completed.'
  echo 'Start menu with: gzqh'
fi
