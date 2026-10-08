#!/usr/bin/env bash
# 在线安装/更新 gzqh（从 GitHub main 分支）
set -euo pipefail
TMP_DIR="$(mktemp -d /tmp/gzqh-portable.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT
curl -fsSL https://github.com/saotu/gzqh-portable/archive/refs/heads/main.tar.gz -o "$TMP_DIR/src.tar.gz"
tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR"
SRC="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"
bash "$SRC/install.sh"
