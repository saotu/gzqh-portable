#!/usr/bin/env bash
# gzqh 安装/更新（唯一安装入口）。
# 全新安装：创建并启用开机自启的 systemd 服务，但不立即启动（先在 gzqh 里配置）。
# 覆盖安装：替换程序文件，保留全部配置和状态；服务原本在运行才会重启。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE=ss-failover
SERVICE_FILE=/etc/systemd/system/ss-failover.service
SCRIPT_FILE=/opt/cf_ss_failover.py
STATE_DIR=/opt/ss-failover
BIN_FILE=/usr/local/bin/gzqh
LOGROTATE_FILE=/etc/logrotate.d/ss-failover

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "请用 root 运行"; exit 1
fi
for f in gzqh cf_ss_failover.py; do
  [ -f "$ROOT_DIR/$f" ] || { echo "安装包不完整，缺少 $f"; exit 1; }
done

need_pkgs=()
command -v python3 >/dev/null 2>&1 || need_pkgs+=(python3)
command -v nft >/dev/null 2>&1 || need_pkgs+=(nftables)
if [ ${#need_pkgs[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "安装依赖: ${need_pkgs[*]}"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need_pkgs[@]}"
  else
    echo "缺少依赖: ${need_pkgs[*]}，请手动安装后重试"; exit 1
  fi
fi
# 注意：不启动/重载 nftables.service，避免清空 nfter 已有规则。

PYTHON_BIN="$(command -v python3)"
python3 -m py_compile "$ROOT_DIR/cf_ss_failover.py"
bash -n "$ROOT_DIR/gzqh"

existing=0
was_active=0
[ -f "$SERVICE_FILE" ] && existing=1
systemctl is-active --quiet "$SERVICE" 2>/dev/null && was_active=1

mkdir -p "$STATE_DIR"
install -m 0755 "$ROOT_DIR/cf_ss_failover.py" "$SCRIPT_FILE.new"
mv -f "$SCRIPT_FILE.new" "$SCRIPT_FILE"
install -m 0755 "$ROOT_DIR/gzqh" "$BIN_FILE.new"
mv -f "$BIN_FILE.new" "$BIN_FILE"
rm -rf "$SCRIPT_FILE".bak_* "$SCRIPT_FILE"c 2>/dev/null || true

# 生成 unit：覆盖安装时沿用旧 unit 里的全部参数（旧版参数会自动迁移）
tmp_unit="$(mktemp "$SERVICE_FILE.XXXXXX")"
if ! "$PYTHON_BIN" "$SCRIPT_FILE" render-unit --python "$PYTHON_BIN" --script "$SCRIPT_FILE" > "$tmp_unit"; then
  rm -f "$tmp_unit"
  echo "现有服务参数无效，请检查 $SERVICE_FILE"; exit 1
fi
chmod 0644 "$tmp_unit"
mv -f "$tmp_unit" "$SERVICE_FILE"

cat > "$LOGROTATE_FILE" <<'ROT'
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
ROT

systemctl daemon-reload
if [ "$existing" -eq 1 ]; then
  if [ "$was_active" -eq 1 ]; then
    systemctl restart "$SERVICE"
    echo "更新完成：配置已保留，服务已重启。"
  else
    echo "更新完成：配置已保留（服务原本未运行，保持未运行）。"
  fi
else
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  echo "全新安装完成：服务已设为开机自启，但尚未启动。"
  echo "请运行 gzqh：2) 添加备用线路 -> 5) 服务控制 -> 1) 启动。"
fi
echo "版本: $("$PYTHON_BIN" "$SCRIPT_FILE" version)    运行: gzqh"
