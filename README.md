# gzqh-portable

Portable, sanitized `gzqh` failover package.

## One-line install and open menu

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/launch.sh)
```

## Install only

```bash
curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/install-online.sh | bash
```

Fresh install behavior:
- A persistent systemd service is created and enabled on boot.
- It is **not started immediately** on first install.
- Open `gzqh`, finish config, then use `7) 服务控制` to start it.

Reinstall behavior:
- Program files are replaced.
- Existing service parameters stay unchanged.

Uninstall:
- Inside `gzqh`, choose `99) 一键卸载`.
- Or run `bash uninstall.sh`.
