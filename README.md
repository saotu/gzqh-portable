# gzqh-portable

nftables 四层故障切换（gzqh / ss-failover）。入口端口的 DNAT 目标在主线和多条备用之间自动切换。

## 一行安装并打开菜单

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/launch.sh)
```

只安装/更新：

```bash
curl -fsSL https://raw.githubusercontent.com/saotu/gzqh-portable/main/install-online.sh | bash
```

> 从 v1 升级：请用上面任意一行命令更新一次（v1 菜单里的 88 读取的是旧 release 包）。升级后配置、主线、锁定状态会自动迁移。

- 全新安装：服务设为开机自启，但不立即启动。打开 `gzqh` → `2) 备用线路` 添加 → `5) 服务控制` 启动。
- 覆盖安装：保留全部参数和状态；服务原本在运行才会重启。
- 安装时不会重载 nftables.service，不会清掉 nfter 已有规则。

## 菜单

```
1) 状态 / 日志            当前模式、线路、主线与各备用连通性、最近切换日志
2) 备用线路               添加 / 删除 / 置顶（支持域名，定期重新解析）
3) 参数设置               入口端口、主线目标、故障检测、恢复检测
4) 切换模式               自动 / 锁定主线 / 锁定某条备用
5) 服务控制               启动 / 停止 / 重启 / systemd 状态 / 立即检测一次
88) 一键更新
99) 一键卸载              卸载前先把入口切回主线
```

命令行：`gzqh status`、`gzqh logs`、`gzqh once`、`gzqh install`。

## 工作方式

- 主线：服务首次运行时读取入口端口现有 DNAT 目标并记住（`/opt/ss-failover/primary.json`）。之后只有「改入口端口」「设置主线」或外部（如 nfter）把入口改到一个非备用目标时才会更新，删除/调整备用线路不会再误把备用当主线。
- 主线连续失败 `FAIL_THRESHOLD` 次 → 切到第一条可用备用；当前备用失败 → 依次换下一条可用备用；主线连续 OK `RECOVER_THRESHOLD` 次 → 切回。所有备用都不可用但主线可达时立即切回。
- 每次切换在一个 nft 事务里完成（新规则 + 删旧规则 + masquerade），不会出现中间态或重复规则。
- 菜单与守护进程通过文件锁串行访问状态，状态文件原子写入。
- 日志只记录状态变化与每分钟心跳。

## 参数（systemd unit 中的 Environment）

| 键 | 默认 | 说明 |
|---|---|---|
| FORWARD_PORT | 10001 | 入口端口 |
| BACKUP_LIST | 空 | 备用线路 `host:port,host:port`，越靠前优先 |
| CHECK_INTERVAL | 0.4 | 在主线时的探测间隔（秒） |
| CHECK_TIMEOUT | 0.06 | TCP 连接超时（秒） |
| FAIL_THRESHOLD | 1 | 连续失败几次切走 |
| BACKUP_CHECK_INTERVAL | 1 | 在备用时的探测间隔（秒） |
| RECOVER_THRESHOLD | 10 | 主线连续 OK 几次切回 |
| HEARTBEAT_INTERVAL | 60 | 心跳日志间隔（秒） |
| DNS_REFRESH_INTERVAL | 300 | 备用域名重新解析间隔（秒） |

旧版的 `BACKUP_HOST` / `BACKUP_PORT` / `RECOVER_INTERVAL` / `PRIMARY_STABLE_COUNT` 已合并或删除，升级时自动迁移。

## Changelog

### 2.0.0 (2026-10-08)
- 修复：删除正在使用的备用、或备用使用域名时，会被误认为主线，导致原主线丢失。
- 修复：菜单锁定/切换与守护进程同时写 state.json 时锁定会被覆盖失效（加文件锁）。
- 修复：state.json 非原子写入，损坏后在备用上无法切回（原子写入 + 主线单独持久化）。
- 修复：参数无校验，输入错误导致服务崩溃重启循环（输入校验；配置错误时服务不再无限重启）。
- 修复：切换由多条 nft 命令组成，中途失败会留下重复规则（改为单事务）。
- 修复：主线故障时第一条备用不可用也会切过去；现在会跳过不可用备用。
- 修复：卸载时若在备用上，入口规则会停留在备用；现在先切回主线。
- 修复：更新会把手动停止的服务重新拉起；更新失败不清理临时目录。
- 修复：同时运行两个守护进程。
- 精简：菜单 13 项合并为 7 项；「强制切回主线」与「恢复自动模式」合并为「切换模式」；新增「锁定主线」。
- 精简：删除 gzqh 中内嵌的第二份 Python 脚本和第二条安装路径；删除未生效的 RECOVER_INTERVAL、PRIMARY_STABLE_COUNT 等参数。
- 日志量从每次探测一行改为只记录变化 + 心跳。

### 2026-07-15
- Fix: while traffic is on backup, primary OK is counted even if every backup is FAIL.
