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

After install, run:

```bash
gzqh
```

## Uninstall

```bash
bash uninstall.sh
```

Persistence:
- Fresh install automatically creates and enables a persistent systemd service.

Menu uninstall:
- Inside `gzqh`, choose `99) 一键卸载`.

Reinstall behavior:
- If an older version already exists, program files are replaced but your current service parameters stay unchanged.
