# 已核实的外部接口

核实日期：2026-10-29。环境：macOS（arm64），Python 3.13.5，SQLite 3.49.1。

## 结论

持仓簿 v0.1.0 只有**一个**运行时外部接口：腾讯行情（`qt.gtimg.cn`）。它只在用户显式要求 `--market live` 或设置 `HOLDBOOK_MARKET=live` 时才会被调用；默认 `off`，测试和演示用 `mock`，两者都不联网。除此之外不调用任何大模型、券商、邮件、翻译、支付接口，也不需要任何 API key。

## 腾讯行情（live 报价）

- **请求地址**：`https://qt.gtimg.cn/q=<代码列表>`，其中 `HOLDBOOK_QUOTE_BASE_URL` 的默认值是 `https://qt.gtimg.cn/q`，实际请求为 `https://qt.gtimg.cn/q=sh600519,sz000001`。这个接口没有参数名，代码列表直接拼在 `q=` 后面。
- **鉴权**：无。不发任何请求头以外的凭证，也没有 cookie。
- **方法**：`GET`。
- **代码写法**：小写交易所前缀 + 6 位数字，例如 `sh600519`、`sz000001`、`sh510300`、`sz159915`。由本工具的规范代码（`600519.SH`）转换得到。
- **响应编码**：`content-type: text/html; charset=GBK`，按 `gb18030` 解码（GBK 的超集，避免生僻字解码失败）。
- **响应格式**：每条一行，形如
  ```
  v_sh600519="1~贵州茅台~600519~1354.00~1330.75~...";
  v_sz000001="51~平安银行~000001~11.54~11.31~...";
  ```
  字段以 `~` 分隔。本工具只依赖其中三个字段（索引从 0 开始）：
  - 索引 1：名称（`贵州茅台`）；
  - 索引 3：最新价（`1354.00`）；
  - 索引 4：昨收（`1330.75`，本工具只用于调试日志，不进入任何计算）。

  未收录的代码返回 `v_pv_none_match="1";`。

- **实测记录（2026-10-29 18:03，本机）**：请求 `https://qt.gtimg.cn/q=sh600519,sz000001,sh510300,sz159915`，HTTP 200，`content-type: text/html; charset=GBK`，2178 字节。解码后前四条为：
  ```
  v_sh600519="1~贵州茅台~600519~1354.00~1330.75~1349.99~...
  v_sz000001="51~平安银行~000001~11.54~11.31~11.30~...
  v_sh510300="1~沪深300ETF华泰柏瑞~510300~4.111~4.152~4.145~...
  v_sz159915="51~创业板ETF易方达~159915~2.535~2.649~2.609~...
  ```
  即索引 1/3/4 与上表一致，价格为正数。

- **官方文档**：**没有**。腾讯没有为这个接口发布公开文档；字段含义靠上面这次实测固定下来。因此代码只读索引 1/3/4，不猜测其他字段；接口若改格式，解析失败按「该代码行情获取失败」处理，不会把错数字写进库。这一点在 README 的「使用前须知」里如实写明。

- **超时、重试、限流**：
  - 超时取 `HOLDBOOK_MARKET_TIMEOUT`（默认 8 秒）；
  - 失败重试 `HOLDBOOK_MARKET_RETRIES`（默认 2 次），退避 1 秒、2 秒；重试只针对超时、连接错误、HTTP 5xx、HTTP 429，HTTP 4xx 不重试；
  - 每次请求最多 60 个代码，两次请求之间至少间隔 1000 ms，一次运行总时长上限 30 秒；
  - 单次运行里同一代码只请求一次。

- **失败的用户可见行为**：单条失败且无缓存价时该标的现价显示 `—`，stderr 打一行警告并带 HTTP 状态码或「连接超时（8 秒）」；全部失败且无缓存时退出码 4（`market_unavailable`），报告不生成。错误信息里不含请求头内容（本接口本来也没有凭证）。

- **为什么保留这个接口**：用户持有的是 A 股/场内 ETF，行情是「按市价估值」的必要输入。但本工具的核心（导入、成本、对账）不需要行情，因此默认关闭，装完就能离线跑通全部核心流程。

## 明确不做的接口

- **券商交易接口 / 自动同步**：不接入，不存券商账号密码，只读用户导出的 CSV。
- **基金公司持仓文件（iShares、Vanguard 等）**：v0.1.0 不抓取，只接受规范 CSV。
- **SEC EDGAR / N-PORT**：v0.1.0 不调用。
- **汇率接口**：v0.1.0 不做多币种换算，所有金额为同一币种（人民币）；报告里不出现汇率假设。
- **大模型**：不调用。报告里的文字全部由固定模板和计算结果生成。

## 使用的公开标准

| 标准 | 用途 | 依据链接 | 核实结果 |
|---|---|---|---|
| RFC 4180 | CSV 的双引号转义、逗号和换行规则 | https://www.rfc-editor.org/rfc/rfc4180.html | 2026-10-29 打开，HTTP 200；实现只用双引号转义规则 |
| RFC 3339 | UTC 时间格式 `YYYY-MM-DDTHH:MM:SSZ` | https://www.rfc-editor.org/rfc/rfc3339.html | 2026-10-29 打开，HTTP 200 |
| ISO 8601（日期部分） | `YYYY-MM-DD` 日期 | https://www.iso.org/iso-8601-date-and-time-format.html | 2026-10-29 访问返回 403（站点反爬）；日期格式按 RFC 3339 的 full-date 实现 |
| SQLite WAL | 并发读写和崩溃恢复 | https://www.sqlite.org/wal.html | 2026-10-29 打开，HTTP 200；本机 SQLite 3.49.1 支持 |
| SQLite PRAGMA | `journal_mode`、`foreign_keys`、`busy_timeout` | https://www.sqlite.org/pragma.html | 2026-10-29 打开，HTTP 200 |
| Python `sqlite3` | 标准库数据库接口 | https://docs.python.org/3/library/sqlite3.html | 本机 Python 3.13.5；目标最低 3.10，接口相同 |
| Python `csv` | 标准库 CSV 读写 | https://docs.python.org/3/library/csv.html | 本机 Python 3.13.5；目标最低 3.10，接口相同 |

## 如何复核「默认不联网」

在仓库根目录运行：

```bash
grep -RInE 'urllib|http\.client|socket' holdbook | grep -v market.py
```

预期只在 `holdbook/market.py` 出现网络调用；`pyproject.toml` 的运行时依赖为空。测试里 `HOLDBOOK_MARKET` 默认 `off`，live 相关用例只连本机 `http.server` 起的假行情服务。
