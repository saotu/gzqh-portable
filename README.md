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
- It is not started immediately on first install.
- Open `gzqh`, finish config, then use `7) 服务控制` to start it.

Reinstall behavior:
- Program files are replaced.
- Existing service parameters stay unchanged.

Menu shortcuts:
- `7) 服务控制（启动/停止/重启/状态）`
- `88) 一键更新`
- `99) 一键卸载`

## Changelog

### 2026-07-15
- Fix: while traffic is on backup, **primary OK is counted even if every backup is FAIL**.
- Before this fix, backup-to-backup rotation `return`ed early and **reset/starved `recover_count`**, so the line could stay on backup forever despite continuous primary OK.
- Fix: sleep interval uses `active.startswith('backup')` so `backup:0` / `backup:1` use `BACKUP_CHECK_INTERVAL`.
