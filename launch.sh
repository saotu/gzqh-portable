#!/usr/bin/env bash
# 一行安装/更新并打开 gzqh 菜单
set -euo pipefail
curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/install-online.sh | bash
exec /usr/local/bin/gzqh </dev/tty
