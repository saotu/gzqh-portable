# gzqh-portable

Portable, sanitized `gzqh` failover package.

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/install-online.sh | bash
```

This installs and immediately starts `gzqh`.

## Offline install

```bash
unzip gzqh-portable.zip
cd gzqh-portable
bash install.sh
gzqh
```

## Uninstall

```bash
bash uninstall.sh
```


Reinstall behavior:
- If an older version already exists, install.sh replaces program files but keeps your current service parameters unchanged.


Persistence:
- Fresh install automatically creates and enables a persistent systemd service.

Menu uninstall:
- Inside `gzqh`, choose `99) 一键卸载`.


Note:
- Using `curl | bash` installs everything first. If your shell is non-interactive, run `gzqh` manually after install.
