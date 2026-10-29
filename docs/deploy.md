# 本地部署与运维

持仓簿是**本地单用户 CLI**，没有服务端、没有容器、没有数据库服务，也不需要域名或 HTTPS。部署就是把 Python 包装进一个隔离环境，并决定数据目录放在哪里。

## 支持的环境

- Python 3.11+；macOS、Linux、Windows。
- 磁盘：程序 <1 MB；数据库约 3.5 MB / 10,000 笔交易（实测，随字段和索引增长）。
- 不需要管理员权限；不要用 `sudo` 运行。

## 安装

```bash
python3 -m venv ~/.venvs/holdbook
~/.venvs/holdbook/bin/pip install .
~/.venvs/holdbook/bin/holdbook --version
```

或：

```bash
pipx install .
```

Windows 把 `bin/` 换成 `Scripts\`，或直接用 `py -m holdbook`。

## 数据目录

默认 `~/.local/share/holdbook`，可用环境变量或 `--data-dir` 改：

```bash
export HOLDBOOK_DATA_DIR="$HOME/Documents/holdbook-data"
holdbook init
```

目录权限由程序设为 `0700`，数据库文件 `0600`。如果放在同步盘（iCloud/Dropbox/OneDrive），注意 SQLite WAL 在同步过程中的一致性风险；更稳妥的是本地目录 + 定期 `backup` 到同步盘。

`HOLDBOOK_DB_PATH` 可以把库单独放到别处；此时 `reports/` 仍在 `data_dir` 下。

## 备份

```bash
holdbook backup
# 默认 ~/.local/share/holdbook/holdbook-backup-20261029-190000.db
holdbook backup --out /Volumes/backup/holdbook-2026-10-29.db
```

`backup` 用 SQLite `VACUUM INTO`，得到一致副本；目标已存在会拒绝（退出 2），不覆盖。建议每月 `check` 后备份一次；升级前必须备份。

本地 cron 示例（可选，不需要服务器）：

```cron
0 20 1 * * /Users/you/.venvs/holdbook/bin/holdbook backup >> /Users/you/Library/Logs/holdbook-backup.log 2>&1
```

## 升级

```bash
holdbook backup
pipx upgrade holdbook   # 或重新 pip install .
holdbook status
holdbook check
```

schema 版本目前是 1；遇到更高版本会拒绝打开（退出 1 `conflict`），不会自动迁移或猜字段。跨大版本升级前先看 `CHANGELOG.md`。

## 恢复

```bash
holdbook reset --yes          # 只在确认要清空时
cp ~/.local/share/holdbook/holdbook-backup-YYYYMMDD-HHMMSS.db ~/.local/share/holdbook/holdbook.db
holdbook status
```

`reset` 只删 `holdbook.db`、`-wal`、`-shm`，不删 `reports/`、备份和原始 CSV。

## 卸载

```bash
pipx uninstall holdbook
rm -rf ~/.local/share/holdbook
```

先确认备份和原始 CSV 已另存；卸载不会替你删数据目录（除非你手动删）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOLDBOOK_DATA_DIR` | `~/.local/share/holdbook` | 数据目录 |
| `HOLDBOOK_DB_PATH` | `<data_dir>/holdbook.db` | 单独指定库文件 |
| `HOLDBOOK_MARKET` | `off` | `off` / `live` / `mock` |
| `HOLDBOOK_QUOTE_BASE_URL` | `https://qt.gtimg.cn/q` | 行情基地址 |
| `HOLDBOOK_MARKET_TIMEOUT` | `8` | 秒，1–60 |
| `HOLDBOOK_MARKET_RETRIES` | `2` | 0–5 |
| `HOLDBOOK_MAX_IMPORT_BYTES` | `104857600` | 单文件字节上限 |
| `HOLDBOOK_MAX_IMPORT_ROWS` | `200000` | 单文件数据行上限 |
| `HOLDBOOK_COST_TOLERANCE_ABS` | `1.00` | 对账成本绝对容差 |
| `HOLDBOOK_COST_TOLERANCE_PCT` | `0.5` | 对账成本百分比容差 |
| `HOLDBOOK_TODAY` | 系统日期 | 覆盖“今天”，影响未来日期校验 |
| `HOLDBOOK_NOW` | 系统 UTC 时间 | 固定时间戳，测试/可复现实验用 |
| `HOLDBOOK_LOG_LEVEL` | `warn` | `debug`/`info`/`warn`/`error` |

没有 API key、cookie 或账号密码。行情接口无鉴权。

## 安全

- 报告是本地 HTML，含真实持仓；转发前自己确认。
- 程序不监听端口、不上传、不写遥测。
- 日志只写 stderr；错误信息不含请求头（本来也没有凭证）。
- 原始 CSV 建议只读保存；持仓簿不修改它们。
