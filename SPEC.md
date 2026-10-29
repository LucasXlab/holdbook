# 持仓簿 Holdbook 规格（v0.1.0）

个人投资者的本地持仓对账工具：把券商导出的交易流水合并成一本可核对的账，用持仓快照对账，指出具体哪只标的、差多少、可能漏了什么；再按移动加权平均成本生成持仓、盈亏、费用和 XIRR 报告。

**本文件是唯一契约。** 命令名、参数名、字段名、枚举值、退出码、数字阈值必须一字不差地实现。实现中遇到本文件没有写到的歧义，按「最简单、能跑通」处理，并在 `docs/deviations.md` 按编号记录；不准悄悄偏离。

## 0. 第一版只做的一条核心流程

```
holdbook init
holdbook import 交割单.csv --account 华泰
holdbook snapshot 持仓.csv --account 华泰
holdbook check
holdbook report --format html --open
```

这五步是唯一要打透的流程。跑通后，用户每次交易或每周核对时重复 `import` → `snapshot` → `check` → `report`。

### 非目标

- 不接券商交易接口，不下单，不自动同步，不保存券商账号密码。
- 不做云同步、账号体系、多人协作，不监听端口。
- 不抓基金季报，不做基金底层穿透。
- 不做期权、期货、外汇保证金、融资融券利息的完整口径；融资标的按普通买卖记账，利息行记为 `fee`。
- 不做税务申报表、境外所得抵免、居民身份判断。
- 不做分钟级行情、盯盘、买卖建议、收益预测。
- 不内置大模型，不生成自然语言点评。
- 不解析券商原始 PDF/Excel；只接受 CSV，列名用别名匹配，见第 4 节。
- 不做多币种换算；所有金额按同一币种（人民币）记录和报告。

## 1. 技术栈与目录

- Python 最低 3.11，生产代码只用标准库：`sqlite3`、`csv`、`json`、`argparse`、`decimal`、`hashlib`、`pathlib`、`datetime`、`urllib`、`html`、`http.server`（仅测试假行情）。
- 运行时第三方依赖：无。开发依赖：无；测试用标准库 `unittest`。
- 金额、数量、价格、成本价全程用 `decimal.Decimal`；禁止用 `float` 做账。金额保留 2 位小数，数量/价格/成本价保留 4 位小数，舍入 `ROUND_HALF_UP`。
- 日期只到日，不做时区换算；`--today` 默认取本机日期。UTC 时间只用于 `created_at`、`fetched_at`。
- 包结构：

```
holdbook/
  holdbook/__init__.py       __version__ = "0.1.0"
  holdbook/__main__.py       python -m holdbook
  holdbook/cli.py            argparse、输出、退出码
  holdbook/config.py         环境变量、路径、Decimal 解析
  holdbook/errors.py         HoldbookError、错误码表
  holdbook/db.py             连接、schema、迁移、事务
  holdbook/models.py         dataclass、枚举
  holdbook/csvio.py          编码、分隔符、表头、别名、逐行校验
  holdbook/ledger.py         重放、成本、已实现盈亏
  holdbook/reconcile.py      对账
  holdbook/metrics.py        XIRR、集中度、费用
  holdbook/market.py         腾讯行情 live/mock、缓存
  holdbook/report.py         text / HTML / JSON
  holdbook/demo.py           内置示例数据
  tests/                     unittest
  docs/  demo/  pyproject.toml
```

- 安装：`pipx install .`、`python3 -m pip install --user .`、或 `python3 -m holdbook`（源码目录）。
- 不引入 Node、Docker、数据库服务；HTML 是单文件，系统浏览器打开。

## 2. 配置

只读 `HOLDBOOK_` 前缀。**不读取** `OPENAI_API_KEY`、`LLM_API_KEY`、`HTTPS_PROXY` 等通用变量作为配置。行情按 Python `urllib` 默认行为使用系统代理，不作为项目配置。

| 变量 | 默认 | 合法值 | 非法时 |
|---|---|---|---|
| `HOLDBOOK_DATA_DIR` | `~/.local/share/holdbook` | 绝对或相对路径 | 无法创建/不可写：退出 1 `data_dir_unwritable` |
| `HOLDBOOK_DB_PATH` | 空（用 `<data_dir>/holdbook.db`） | 文件路径 | 父目录不可创建/不可写：退出 1 `data_dir_unwritable` |
| `HOLDBOOK_MARKET` | `off` | `off` / `live` / `mock` | 退出 2 `bad_config`，错误带变量名和收到的值 |
| `HOLDBOOK_QUOTE_BASE_URL` | `https://qt.gtimg.cn/q` | `http://` 或 `https://` 开头 | 退出 2 `bad_config` |
| `HOLDBOOK_MARKET_TIMEOUT` | `8` | 整数 1–60（秒） | 退出 2 `bad_config` |
| `HOLDBOOK_MARKET_RETRIES` | `2` | 整数 0–5 | 退出 2 `bad_config` |
| `HOLDBOOK_MAX_IMPORT_ROWS` | `200000` | 整数 1–2000000 | 退出 2 `bad_config` |
| `HOLDBOOK_MAX_IMPORT_BYTES` | `104857600` | 整数 1024–104857600 | 退出 2 `bad_config` |
| `HOLDBOOK_COST_TOLERANCE_PCT` | `0.5` | 数字 0–100 | 退出 2 `bad_config` |
| `HOLDBOOK_COST_TOLERANCE_ABS` | `1.00` | 数字 ≥ 0 | 退出 2 `bad_config` |
| `HOLDBOOK_TODAY` | 空（用系统日期） | `YYYY-MM-DD` | 退出 2 `bad_config` |
| `HOLDBOOK_NOW` | 空（用系统 UTC 时间） | 带时区的 ISO 8601，例如 `2026-10-29T21:00:00Z` | 退出 2 `bad_config` |
| `HOLDBOOK_LOG_LEVEL` | `warn` | `debug` / `info` / `warn` / `error` | 退出 2 `bad_config` |

- 命令行 `--data-dir` 覆盖 `HOLDBOOK_DATA_DIR`；`--today` 覆盖 `HOLDBOOK_TODAY`。`HOLDBOOK_NOW` 只影响 `created_at`、`fetched_at`、报告/备份文件名的时间戳，不改变 `--today` 的日期判断。
- 配置错误发生在建库、读文件之前；只写 stderr，不改数据。
- 数据目录创建时权限 `0700`；数据库文件创建时权限 `0600`；备份文件 `0600`。
- 日志写 stderr。`warn` 只输出警告和错误；`info` 额外输出每步摘要；`debug` 额外输出耗时。日志行格式 `YYYY-MM-DDTHH:MM:SSZ LEVEL message`，不输出金额明细，不输出密钥（本工具没有密钥）。

## 3. 数据库

SQLite 3，文件 `<data_dir>/holdbook.db`。打开时执行：
`PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`、`PRAGMA busy_timeout=5000`、`PRAGMA quick_check`。
`quick_check` 不返回 `ok` → 退出 1 `db_corrupt`，提示先备份再重建。`meta.schema_version` 不是 `1` → 退出 1 `conflict`，不自动改库。

### 3.1 `meta`

| 字段 | 类型 | 约束 |
|---|---|---|
| `key` | TEXT | PRIMARY KEY；只允许 `schema_version`、`created_at` |
| `value` | TEXT | NOT NULL |

### 3.2 `accounts`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `name` | TEXT | NOT NULL UNIQUE；去首尾空白后 1–40 字符；不含 CR/LF |
| `broker` | TEXT | NOT NULL DEFAULT ''；0–40 字符 |
| `created_at` | TEXT | NOT NULL；UTC `YYYY-MM-DDTHH:MM:SSZ` |

### 3.3 `instruments`

| 字段 | 类型 | 约束 |
|---|---|---|
| `code` | TEXT | PRIMARY KEY；规范代码，见 3.3.1 |
| `name` | TEXT | NOT NULL DEFAULT ''；0–40 字符 |
| `board` | TEXT | NOT NULL；枚举 `sh_main`、`sz_main`、`sz_gem`、`sh_star`、`etf`、`bond_cb`、`cash`、`unknown` |
| `updated_at` | TEXT | NOT NULL；UTC 时间 |

#### 3.3.1 代码规范化

输入可为 `600519`、`sh600519`、`SH600519`、`600519.SH`、`600519.sh`。规则：

1. 去掉首尾空格和 BOM，转大写。
2. 匹配 `^(SH|SZ)?(\d{6})(\.(SH|SZ))?$`；不匹配时只接受 `CASH.CASH`，其余返回 `bad_code`。
3. 无显式交易所时，按 6 位数字前缀推断；显式 `SH`/`SZ` 与推断不一致 → `bad_code`（防止 `000001.SH` 这类错码入库）：

| 6 位前缀 | 交易所 | board |
|---|---|---|
| `600`、`601`、`603`、`605` | SH | `sh_main` |
| `688`、`689` | SH | `sh_star` |
| `000`、`001`、`002`、`003` | SZ | `sz_main` |
| `300`、`301` | SZ | `sz_gem` |
| `510`–`518`、`560`–`563`、`588` | SH | `etf` |
| `159`–`169` | SZ | `etf` |
| `110`、`111`、`113`、`118` | SH | `bond_cb` |
| `123`、`127`、`128` | SZ | `bond_cb` |

4. 北交所（`4`/`8`/`920` 开头）、B 股、基金中其他前缀 → `bad_code`。v0.1.0 不猜。
5. 规范代码 = `数字.交易所`。
6. 同一 6 位数字的 `SH`/`SZ` 写成不同代码时，按显式交易所分别建；但只有推断一致的才能通过第 3 条。

### 3.4 `transactions`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `account_id` | INTEGER | NOT NULL REFERENCES accounts(id) |
| `trade_date` | TEXT | NOT NULL；`YYYY-MM-DD`，真实日历日 |
| `kind` | TEXT | NOT NULL；枚举见 3.4.1 |
| `code` | TEXT | NOT NULL REFERENCES instruments(code) |
| `quantity` | TEXT | NOT NULL；带符号十进制字符串，4 位小数；现金事件 `0.0000` |
| `price` | TEXT | NOT NULL；十进制字符串，4 位小数；现金事件 `0.0000` |
| `amount` | TEXT | NOT NULL；带符号金额，2 位小数；语义见 3.4.2 |
| `fee_commission` | TEXT | NOT NULL DEFAULT `0.00`；≥ 0 |
| `fee_stamp` | TEXT | NOT NULL DEFAULT `0.00`；≥ 0 |
| `fee_transfer` | TEXT | NOT NULL DEFAULT `0.00`；≥ 0 |
| `fee_other` | TEXT | NOT NULL DEFAULT `0.00`；≥ 0 |
| `note` | TEXT | NOT NULL DEFAULT ''；0–200 字符 |
| `source_file` | TEXT | NOT NULL；basename，1–200 字符 |
| `source_line` | INTEGER | NOT NULL；源文件行号，表头为 1 |
| `row_hash` | TEXT | NOT NULL UNIQUE；64 位小写十六进制 |
| `created_at` | TEXT | NOT NULL；UTC 时间 |

索引：`(account_id, trade_date, id)`、`(code)`、`(source_file)`。

#### 3.4.1 `kind` 与识别

原始「操作/摘要」文本去空白、转小写后，按以下顺序匹配，先命中先用：

匹配前先做两条排除，避免把税和送股记反：

- 文本含 `税`、`扣税`、`代扣` → `fee`（`红利税`、`股息税`、`代扣税` 都走这里，金额为负）。
- 文本含 `送`、`转增` → `bonus_share`（`分红送股` 走这里，不按现金分红；如果同一行还含现金分红词，见下面的拆行规则）。

然后按顺序匹配：

| 顺序 | 包含任一词 | kind |
|---|---|---|
| 1 | `红股`、`送股`、`转增`、`送转`、`bonus` | `bonus_share` |
| 2 | `红利`、`股息`、`分红`、`dividend` | `dividend` |
| 3 | `配股`、`rights` | `rights_issue` |
| 4 | `中签`、`新股` | `ipo_allot` |
| 5 | `利息`、`费用`、`手续费`、`佣金`、`规费` | `fee` |
| 6 | `银证转入`、`银行转入`、`转入` | `transfer_in` |
| 7 | `银证转出`、`银行转出`、`转出` | `transfer_out` |
| 8 | `买入`、`证券买入`、`buy` | `buy` |
| 9 | `卖出`、`证券卖出`、`sell` | `sell` |

- `申购` 单独出现的行（未中签的申购冻结/扣款）→ `other`，只入库不计持仓；`中签`/`新股` 行才按 `ipo_allot` 入账。
- 都不命中 → `other`，照常入库，摘要计「未识别」，stderr 打警告，不参与账。
- 同一行同时含现金分红词和送股/转增词 → `bad_row`，提示拆成两行（一行现金分红、一行送股），避免静默丢掉现金部分。
- `dividend`：现金分红，`quantity=0`，`price=0`，`amount` 取正数。
- `bonus_share`：`quantity` 取正数，`price=0`，`amount=0`，四个费用为 0。
- `rights_issue`：`quantity` 必须 >0，`price` 为配股价，`amount=-quantity*price`；数量为 0 → `bad_row`。
- `ipo_allot`：按 `buy` 入账，但保留 kind。
- `fee`：`quantity=0`，`price=0`，`amount=-费用合计`，`fee_other=费用合计`，其余费用 0。
- `transfer_in`/`transfer_out`：只影响现金，不影响持仓；`amount` 正/负。
- `other`：不参与账。

#### 3.4.2 金额语义（关键）

- `amount` 只表示**本金现金流，不含费用**：
  - `buy` / `ipo_allot` / `rights_issue`：`amount = -gross`；
  - `sell`：`amount = +gross`；
  - `dividend`：`amount = +现金分红`；
  - `bonus_share`：`0.00`；
  - `fee`：`-费用合计`；
  - `transfer_in`：正数；`transfer_out`：负数；
  - `other`：`0.00`。
- `gross = abs(quantity) * price`，保留 2 位小数。
- 四个费用字段都存正数。交易净现金流：
  - 买入类：`amount - fee_total`（负数）；
  - 卖出：`amount - fee_total`（正数）；
  - 分红：`amount`；
  - 费用行：`amount`（已经是负的，不再减四个费用字段，否则重复）。
- 报告中的费用合计 = 所有行四个费用字段之和（费用行只通过 `fee_other` 计一次）。

#### 3.4.3 去重 `row_hash`

```
row_hash = sha256(
  account_name + US + trade_date + US + kind + US + code + US +
  quantity + US + price + US + amount + US +
  fee_commission + US + fee_stamp + US + fee_transfer + US + fee_other + US +
  occurrence
)
```
`US` 是 `\x1f`。拼接前统一格式化：`quantity`、`price` 用 `format(x, '.4f')`；`amount` 和四个费用用 `format(x, '.2f')`；`code` 用规范码；`account_name` 用去首尾空白后的名称；`kind` 用规范 kind；`occurrence` 用十进制无前导零。`occurrence` 是从 0 开始的出现序号：同一文件内上述字段完全相同的第 2 行序号为 1。`note`、`source_file`、`source_line` 不参与 hash。效果：

- 同一文件重复导入 → 全部 `skipped`；
- 同一文件内两行真实相同成交 → occurrence 不同，都入库；
- 两个不同文件各有一行相同内容 → 第二次 `skipped`（幂等）。

### 3.5 `snapshots`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `account_id` | INTEGER | NOT NULL REFERENCES accounts(id) |
| `as_of_date` | TEXT | NOT NULL；`YYYY-MM-DD` |
| `code` | TEXT | NOT NULL REFERENCES instruments(code) |
| `quantity` | TEXT | NOT NULL；≥ 0，4 位小数 |
| `cost_price` | TEXT | NULL 或 ≥ 0，4 位小数 |
| `market_value` | TEXT | NULL 或 ≥ 0，2 位小数 |
| `source_file` | TEXT | NOT NULL；basename |
| `created_at` | TEXT | NOT NULL；UTC 时间 |

唯一约束 `(account_id, as_of_date, code)`。同账户同日重新导入时按代码覆盖，并统计 `overwritten`。

### 3.6 `prices`

| 字段 | 类型 | 约束 |
|---|---|---|
| `code` | TEXT | NOT NULL REFERENCES instruments(code) |
| `date` | TEXT | NOT NULL；`YYYY-MM-DD` |
| `close` | TEXT | NOT NULL；> 0 且 ≤ 1000000，4 位小数 |
| `source` | TEXT | NOT NULL；`live` / `mock` / `manual` |
| `fetched_at` | TEXT | NOT NULL；UTC 时间 |
| PRIMARY KEY | | `(code, date)` |

### 3.7 `imports`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `account_name` | TEXT | NOT NULL；1–40 字符 |
| `file` | TEXT | NOT NULL；basename，1–200 字符 |
| `imported_at` | TEXT | NOT NULL；UTC 时间 |
| `rows_read` | INTEGER | NOT NULL；数据记录数（不含表头） |
| `added` | INTEGER | NOT NULL；实际新增笔数 |
| `skipped` | INTEGER | NOT NULL；重复跳过笔数 |
| `errors` | INTEGER | NOT NULL；错误行数 |
| `status` | TEXT | NOT NULL；`ok` / `partial` / `error` |
| `message` | TEXT | NOT NULL DEFAULT ''；0–200 字符 |

每次 `import` 的每个文件写一行；`--dry-run` 不写。`status=error` 用于文件级失败（此时没有业务数据写入）；行级错误写 `partial`；全部成功写 `ok`。`status` 和 `report` 的「数据来源」从这里取 `rows_read`。写入 `imports` 与业务数据在同一个事务；文件级失败时账户创建、交易、imports 全部回滚。

## 4. CSV 输入

### 4.1 通用规则

- 编码：`auto` 先按 UTF-8 严格解码；失败用 `gb18030`；`--encoding` 可强制 `utf-8` 或 `gb18030`。
- 分隔符：逗号、制表符、分号；取表头行出现次数最多的；并列时逗号优先；都没有按逗号。
- 表头：前 5 行内找第一行包含「日期」类列名的行；找不到 → 2 `missing_columns`。表头不受「以 # 开头为注释」影响。
- 表头去首尾空格、全角转半角、转小写后匹配；重复表头 → 2 `duplicate_column`。
- 数据行：空行跳过；以 `#` 开头的整行跳过；字段数少于表头补空，多于表头 → 错误行。
- 单文件最大 `HOLDBOOK_MAX_IMPORT_BYTES`（默认 104857600 字节，即 100 MiB）；超过 → 2 `bad_csv`。行数上限 `HOLDBOOK_MAX_IMPORT_ROWS`（默认 200000，硬上限 2000000），按数据记录计；超过 → 2 `bad_csv`，整个命令不写库。检查顺序：先 `os.stat` 看字节数，再逐行读；超限时不完整解析。
- 数字：允许千分位逗号、全角数字、前导 `+`、括号负数 `(1,234.50)`；不允许 `NaN`、`Inf`、科学计数法、货币符号、百分号。
- 日期：`YYYY-MM-DD`、`YYYY/MM/DD`、`YYYYMMDD`、`YYYY-MM-DD HH:MM:SS`；其余为错误行。未来日期相对 `--today` 判断。
- 文本去首尾空格；内部换行允许，但总长度不超过 200 字符。

### 4.2 交割单 CSV（`import`）

必需列：日期、操作。其余列缺失按空处理。

| 逻辑列 | 别名（去空格、全角转半角、小写后比较） |
|---|---|
| 日期 | `成交日期`、`发生日期`、`交易日期`、`日期`、`交收日期`、`date` |
| 代码 | `证券代码`、`股票代码`、`代码`、`symbol`、`code` |
| 名称 | `证券名称`、`股票名称`、`名称`、`name` |
| 操作 | `操作`、`买卖标志`、`业务名称`、`摘要`、`交易类型`、`business` |
| 数量 | `成交数量`、`发生数量`、`数量`、`qty`、`quantity` |
| 价格 | `成交价格`、`成交均价`、`价格`、`price` |
| 金额 | `成交金额`、`发生金额`、`金额`、`amount` |
| 佣金 | `手续费`、`佣金`、`commission` |
| 印花税 | `印花税`、`tax` |
| 过户费 | `过户费`、`transferfee` |
| 其他费用 | `其他费用`、`规费`、`otherfee` |

逐行规则：

1. 日期、操作必须可解析；否则错误行。
2. `kind` 按 3.4.1 识别。`other` 不要求代码、数量、价格、金额。
3. 非 `other` 且非现金类（`fee`、`transfer_in`、`transfer_out`）时，代码必须能规范化；否则错误行。
4. 现金类（`fee`、`transfer_in`、`transfer_out`）缺少代码时用 `CASH.CASH`；`other` 缺少代码时也用 `CASH.CASH`。
5. 交易类（`buy`、`sell`、`ipo_allot`、`rights_issue`）：
   - 数量、价格、金额至少有一个非空；否则错误行。
   - 有数量和价格：`gross=abs(quantity)*price`；若金额非空且 `abs(abs(amount)-gross)>0.02` → 错误行 `bad_row`，错误里给两者。
   - 只有金额和数量：`price=abs(amount)/quantity`，保留 4 位；`gross=abs(amount)`。
   - 只有数量和价格：`gross=abs(quantity)*price`。
   - 只有金额没有数量：错误行。
   - `quantity` 按 kind 定符号：买入类为正，卖出为负；输入负数取绝对值并警告。
   - `amount` 按 3.4.2 重新计算，忽略 CSV 的符号；若 CSV 金额与 `gross` 差 ≤0.02 视为一致。
6. `dividend`：金额必须非空且 >0；`quantity=0`、`price=0`。
7. `bonus_share`：数量必须非空且 >0；`price=0`、`amount=0`；四个费用必须为 0。
8. `fee`：金额非空；费用合计 = `abs(amount)`；`fee_other` 记该值；`amount=-该值`。
9. `transfer_*`：金额非空；`quantity=0`、`price=0`。
10. 费用字段：非空则必须 ≥0，最多 2 位小数；空为 0。`fee` 行若同时给了四个费用字段，以 `abs(amount)` 覆盖 `fee_other`，其余置 0。
11. 数量绝对值 > 1e9、价格 > 1e9、金额绝对值 > 1e12 → 错误行。
12. 名称超过 40 字符时截断到 40 字符并告警；`note`（原始操作文本）超过 200 字符时截断到 200 字符。
13. 行级错误：跳过该行，继续解析；其他合法行仍入库；退出码 2。摘要写明 `已入库，但有 K 行未导入`。
14. 文件级错误（编码失败、缺表头/必填列、行数超限、文件不存在）：整个命令不写库，退出码 1 或 2。

`--dry-run` 完整解析和校验，不写库，摘要与真实导入一致。多文件导入：按参数顺序解析；任一文件级错误 → 全部不写；行级错误不阻止其他文件。

### 4.3 持仓快照 CSV（`snapshot`）

必需列：代码、数量。别名：

| 逻辑列 | 别名 |
|---|---|
| 代码 | 同 4.2 |
| 名称 | 同 4.2 |
| 数量 | `持仓数量`、`股票余额`、`当前持仓`、`数量`、`qty`、`quantity` |
| 成本价 | `成本价`、`参考成本价`、`持仓成本`、`costprice` |
| 市值 | `市值`、`参考市值`、`marketvalue` |

- 数量必须 ≥0；0 表示已清仓，照常写入。
- 成本价、市值可空；负数为错误行。
- 同一文件同代码出现多次 → 取最后一行，stderr 警告。
- 未来日期 → 2 `bad_date`。
- 逐行校验与 4.2 相同：日期、代码、数量、成本价、市值、文本长度、行数上限、文件大小；错误行跳过并计入 `errors`，其他行入库，退出码 2；文件级错误不写库。

### 4.4 手工价格 CSV（`report --prices`）

必需列：`code,close`；可选列：`date`。

- `code` 按 3.3.1 规范化；`close` 必须 >0 且 ≤1000000，4 位小数；`date` 缺省用报告的 `as_of`。
- 同一文件同代码同日期重复 → 最后一行生效。
- `date` 晚于报告 `as_of` 的行忽略并告警；`date` 早于 `as_of` 可用。
- 写入/覆盖 `prices`，`source=manual`，`fetched_at` 用当前时间。
- 行级错误：该行跳过，其他行可用，退出码 2；文件级错误（缺列、无法解码）→ 2 `bad_csv` / `missing_columns`，报告不生成。

## 5. 命令

全局参数（写在子命令前）：`--data-dir PATH`、`--json`、`--today YYYY-MM-DD`、`--quiet`、`-v/--verbose`、`--version`。`--version` 等价于 `holdbook version`，立即退出 0。`--quiet` 抑制进度和摘要，只保留结果、警告和错误。

退出码：

| 码 | 含义 |
|---|---|
| 0 | 成功；`check` 有差异但未加 `--strict` 也算成功 |
| 1 | 运行时错误：数据目录不可写、数据库损坏/版本不支持、文件不存在、报告写入失败、无数据 |
| 2 | 输入/配置错误：参数、CSV、日期、代码、目标、环境变量 |
| 3 | `check --strict` 发现差异 |
| 4 | 行情不可用，且没有可用缓存价 |

错误输出：stderr 一行 `错误: <中文说明> (code=<机器码>)`；`--json` 时改为 stdout 一行 `{"error":"<中文说明>","code":"<机器码>"}`，退出码不变。错误信息不包含完整文件内容、金额明细、请求头或密钥。

### 5.1 `holdbook init`

- 创建数据目录（`0700`）、数据库（`0600`）、`reports/`；写入 `meta`。
- 已存在有效库：幂等，不动数据，输出 `已就绪`。
- 已存在 SQLite 文件但缺 `meta.schema_version`：退出 1 `conflict`，不覆盖。
- 输出：
```
数据目录: /abs/path
数据库: /abs/path/holdbook.db
已就绪（schema_version=1）
```
- JSON：`{"ok":true,"dbPath":"/abs/path/holdbook.db","schemaVersion":1}`。

### 5.2 `holdbook import FILE... --account NAME [--broker NAME] [--encoding auto|utf-8|gb18030] [--dry-run]`

- `--account` 1–40 字符，去首尾空白，不能为空或含换行；不存在则创建，输出提示 `新建账户`。
- `--broker` 0–40 字符；提供时更新账户的 broker。
- 输出（每个文件一段）：
```
账户: 华泰
文件: 交割单_2026.csv（128 行，表头第 1 行）
新增 126 笔，跳过重复 2 笔，错误 0 行
按类型: buy 72, sell 40, dividend 6, bonus_share 3, fee 5
未识别: 0 行
```
- 多文件最后一行：`合计: 新增 N 笔，跳过 M 笔，错误 K 行`。
- 有错误行：最多显示 10 条 `错误: 第 37 行：数量不是数字「-」 (code=bad_row)`，其余 `还有 N 行错误未显示`；退出 2。
- JSON：
```json
{"ok":true,"files":[{"file":"交割单.csv","rows":128,"added":126,"skipped":2,"errors":[{"line":37,"message":"数量不是数字「-」"}]}],"totals":{"added":126,"skipped":2,"errors":0},"accountsCreated":["华泰"]}
```
`--dry-run` 时 `ok=true`、`added` 为 0，新增 `"wouldAdd":126`；不写库。
- 有行级错误但合法行已提交时，仍然只输出这一份摘要 JSON（不要另输出 `{"error":...}`），并加 `"committed":true`；退出码 2。文件级失败没有摘要时，才输出错误对象。

### 5.3 `holdbook snapshot FILE --account NAME [--as-of YYYY-MM-DD] [--dry-run]`

- `--account` 必须已存在；不存在 → 2 `unknown_account`。
- `--as-of` 默认 `--today`；未来 → 2 `bad_date`。
- 同账户同日同代码覆盖；`overwritten` 计数。
- 输出：`快照: 华泰 2026-10-29，8 只标的`；覆盖时 `提示: 已覆盖 3 条`。
- JSON：`{"ok":true,"account":"华泰","asOf":"2026-10-29","rows":8,"overwritten":3,"errors":[]}`。
- `--dry-run` 不写库，`rows` 为将写入行数。
- `errors` 数组元素固定为 `{"line": 37, "message": "数量不是数字「-」"}`。

### 5.4 `holdbook check [--account NAME] [--as-of YYYY-MM-DD] [--strict]`

- 默认每个账户用最新快照（同日期取最大 `id`）；`--account` 只查该账户，账户不存在 → 2 `unknown_account`；`--as-of` 指定日期时每个被查账户必须有该日快照，否则 2 `no_data`，错误列出账户和最近快照日期。
- 默认模式下各账户快照日期可以不同：顶层 JSON `asOf` 取这些日期的最大值，每个账户对象另给 `snapshotDate`；文本报告在每个账户标题里写自己的日期。
- 计算见第 6.3 节。
- 文本输出：
```
对账: 华泰 @ 2026-10-29（快照来源 持仓_20261029.csv）
标的 12 只，一致 9 只，差异 3 只

600519.SH 贵州茅台  账上 200 股 / 快照 200 股  一致
000001.SZ 平安银行  账上 1,000 股 / 快照 1,300 股  账上少 300 股：可能漏导入买入或送转
                      提示: 差值正好等于 2026-06-12 送转的 300 股，可能漏导入该送转行
510300.SH 沪深300ETF 账上 5,000 股 / 快照 5,000 股  成本价不一致：差 1.2812
```
- 有差异时最后一行：`结论: 有 3 只对不上，先按上面提示逐条核对。`
- `--strict` 有任一 `status != matched` → 3；否则 0。
- JSON：
```json
{"ok":true,"asOf":"2026-10-29","strict":false,"differed":3,
 "accounts":[{"account":"华泰","snapshot":"持仓_20261029.csv","snapshotDate":"2026-10-29","matched":9,"differed":3,
 "items":[{"code":"000001.SZ","name":"平安银行","qtyCalc":"1000.0000","qtySnapshot":"1300.0000","avgCostCalc":"11.5400","costSnapshot":"11.5400","qtyDiff":"-300.0000","costDiff":"0.0000","status":"qty_missing","hints":["差值正好等于 2026-06-12 送转的 300 股，可能漏导入该送转行"]}]}]}
```
- 无账户或无快照：退出 0，文本 `还没有数据：先运行 holdbook import 和 holdbook snapshot`，JSON `{"ok":true,"asOf":"...","accounts":[],"differed":0}`。

### 5.5 `holdbook report [--account NAME] [--as-of YYYY-MM-DD] [--market off|live|mock] [--prices FILE] [--format text|html] [--out PATH] [--open]`

- 默认 `--format text`；`--format html` 写文件；`--json` 只输出 JSON，不写 text/HTML。
- 数据截止 `--as-of` 默认 `--today`；持仓由账本重放得到，不依赖快照。
- 价格优先级（逐代码）：`--prices` 手工价 > `prices` 表中 `date <= as_of` 的最新缓存 > `--market live/mock` 请求 > 无价格。
- 默认 HTML 输出 `<data_dir>/reports/report-<as_of>-<YYYYMMDD-HHMMSS>.html`；时间戳取 `HOLDBOOK_NOW`（未设置则系统时间）的 UTC 时间，格式 `%Y%m%d-%H%M%S`。同秒已存在同名文件时追加 `-2`、`-3`，不覆盖。
- `--out` 只允许与 `--format html` 一起使用；`--format text` 时给 `--out` → 2 `bad_config`。
- `--out -` 把 HTML 写到 stdout；此时不打印其他 stdout 文本，摘要写 stderr；`--json` 与 `--out -` 同时出现 → 2 `bad_config`。
- `--json` 与 `--format html` 同时出现时，只输出 JSON，不写 HTML 文件；`--json` 与 `--open` 同时出现 → 2 `bad_config`；`--json` 与 `--out` 同时出现 → 2 `bad_config`。
- `--open` 隐含 `--format html`；与 `--format text` 或 `--out -` 同时出现 → 2 `bad_config`。用 `open` / `xdg-open` / `cmd /c start` 打开；失败只警告，不影响退出码。
- `--out` 指向已存在的普通文件时原子覆盖（临时文件 + `os.replace`）；指向目录 → 1 `file_not_writable`。
- 无任何持仓/交易 → 1 `no_data`。
- 报告字段见第 7 节。

### 5.6 `holdbook explain CODE [--account NAME] [--as-of YYYY-MM-DD]`

- 规范代码；不存在 → 2 `bad_code`；无交易 → 1 `no_data`。
- 未给 `--account` 且库中多于一个账户 → 2 `bad_config`，提示加 `--account`。
- 文本：逐笔表 + 重放结果；JSON：
```json
{"ok":true,"code":"600519.SH","name":"贵州茅台","account":"华泰",
 "rows":[{"date":"2025-03-10","kind":"buy","quantity":"100.0000","price":"1580.0000","amount":"-158000.00","fees":"5.00","qtyAfter":"100.0000","avgCostAfter":"1580.0500"}],
 "final":{"quantity":"0.0000","avgCost":null,"realizedPnl":"13990.00"}}
```

### 5.7 `holdbook status`

- 无库 → 1 `no_data`；库存在但没有任何账户/交易 → 0，输出零值。
- 输出库路径、schema、账户数、每账户交易笔数/最近导入时间（`imports.imported_at` 最大值）/最近快照日期、标的数、行情模式。
- JSON：`{"ok":true,"dbPath":"...","schemaVersion":1,"market":"off","accounts":[{"name":"华泰","transactions":126,"lastImportAt":"...","lastSnapshotDate":"2026-10-29"}],"instruments":8}`。

### 5.8 `holdbook backup [--out PATH]`

- 无库 → 1 `no_data`。`VACUUM INTO` 一致副本；默认 `<data_dir>/holdbook-backup-<YYYYMMDD-HHMMSS>.db`，时间戳取 `HOLDBOOK_NOW`（未设置则系统 UTC 时间）。
- 目标已存在且是文件 → 覆盖？不覆盖，退出 2 `conflict`；是目录 → 1 `file_not_writable`。
- JSON：`{"ok":true,"path":"...","bytes":123456}`。

### 5.9 `holdbook reset --yes`

- 无 `--yes` → 2 `conflict`，提示命令会删除全部数据。
- 无库 → 1 `no_data`。删除 `holdbook.db`、`-wal`、`-shm`；保留目录和其他文件。
- JSON：`{"ok":true,"deleted":["..."]}`。

### 5.10 `holdbook version`

- 输出 `holdbook 0.1.0`；JSON：`{"ok":true,"version":"0.1.0","python":"3.11.0"}`。
- 全局 `--version` 等价于 `holdbook version`。

## 6. 账本、对账与指标

### 6.1 重放顺序与状态

按 `trade_date` 升序、`id` 升序重放。每个 `(account, code)` 维护：
`qty`（Decimal 4 位）、`total_cost`（Decimal 2 位，含买入费用）、`realized`（2 位）、`dividends`（2 位）。

逐笔规则：

| kind | 数量 | 成本 | 已实现/分红 |
|---|---|---|---|
| `buy` / `ipo_allot` | `qty += quantity` | `total_cost += gross + fees` | — |
| `sell` | `qty -= abs(quantity)` | `cost_out = avg_cost * abs(quantity)`；`total_cost -= cost_out` | `realized += (gross - fees) - cost_out` |
| `bonus_share` | `qty += quantity` | 不变 | — |
| `rights_issue` | `qty += quantity` | `total_cost += gross + fees` | — |
| `dividend` | 不变 | 不变 | `dividends += amount` |
| `fee` | 不变 | 不变 | —（只进费用合计） |
| `transfer_*` | 不变 | 不变 | — |
| `other` | 不变 | 不变 | — |

- `avg_cost = total_cost / qty`，qty=0 时为 `null`。
- 卖出数量 > 当前持仓 → 该账户账本错误：退出 2 `ledger_negative`，错误带账户、代码、日期、可卖数量。`check`/`report`/`explain` 遇到即失败。
- 数量、成本、已实现用 Decimal 原值计算；输出时金额 2 位、数量/成本价 4 位，HALF_UP。
- 清仓时 `total_cost=0`、`avg_cost=null`，`realized` 保留。
- 交易日期晚于 `as_of` 的不参与。

### 6.1.1 精度与算例

- 内部用 Decimal，保留 12 位小数；输出金额 2 位、数量/成本价 4 位，`ROUND_HALF_UP`。
- 算例：买入 100 股 @10.00，佣金 5.00 → `gross=1000.00`，`total_cost=1005.00`，`avg_cost=10.0500`。
- 卖出 40 股 @12.00，佣金 6.00 → `gross=480.00`，`cost_out=40×10.05=402.00`，`realized=(480-6)-402=72.00`；剩余 60 股 `total_cost=603.00`，`avg_cost=10.0500`。
- 送转 10 股 → 70 股，`total_cost=603.00`，`avg_cost=8.6143`。
- 再买入 60 股 @9.00，佣金 5.00 → 130 股，`total_cost=603+540+5=1148.00`，`avg_cost=8.8308`。

### 6.2 对账范围与状态

对每个被查账户、`as_of` 快照：
- 候选代码 = 快照中的代码 ∪ 重放后 `qty != 0` 的代码。
- 对每个代码计算 `qty_calc`、`avg_cost_calc`；快照取 `qty_snap`、`cost_snap`。
- 记 `has_position_tx` = 该账户该代码在 `as_of` 前存在 `buy`/`sell`/`ipo_allot`/`rights_issue`/`bonus_share` 中任一笔。
- 状态判定按顺序：
  1. 快照无该代码、`qty_calc=0` → 不出结果；
  2. 快照无该代码、`qty_calc>0` → `only_ledger`；
  3. 快照有、`qty_calc=0`、`qty_snap>0`：`has_position_tx=false` → `only_snapshot`；否则 → `qty_missing`；
  4. 快照有、`qty_calc=0`、`qty_snap=0` → 不出结果；
  5. `qty_calc != qty_snap`：`qty_calc < qty_snap` → `qty_missing`；否则 `qty_extra`；
  6. 数量相等、`cost_snap` 为空 → `no_cost`；
  7. 数量相等、`abs(avg_cost_calc - cost_snap) <= max(HOLDBOOK_COST_TOLERANCE_ABS, cost_snap * PCT / 100)` → `matched`；
  8. 否则 → `cost_off`。
- `qtyDiff = qty_calc - qty_snap`；`costDiff = avg_cost_calc - cost_snap`（空值输出 `null`）。
- `matched` 才计入 `matched` 数；其余计入 `differed`。`items` 按代码升序排列。

### 6.3 提示规则

每个差异最多 2 条，按顺序生成，去重：
1. `qty_missing` 且该代码在 `as_of` 前 90 天内有 `bonus_share`，且差值等于最近一次送转数量（4 位内）→ `差值正好等于 <date> 送转的 <qty> 股，可能漏导入该送转行`。
2. `qty_missing` 且该代码在 `as_of` 前 90 天内有 `dividend` → `近期有分红，确认是否漏导入除权日之后的行`。
3. `only_ledger` 且最后一条交易早于 `as_of` 30 天以上 → `该标的最后交易在 <date>，可能已卖出但未导入`。

### 6.4 XIRR

- 现金流（只取 `trade_date <= as_of`）：
  - `buy`/`ipo_allot`/`rights_issue`：`-(gross + fees)`；
  - `sell`：`gross - fees`；
  - `dividend`：`+amount`；
  - `fee`：`-abs(amount)`；
  - `transfer_in/out`、`bonus_share`：不计。
- 期末价值：`as_of` 当日持仓市值；无价格的标的用 `total_cost` 替代。若期末价值为 0 且没有正现金流 → 无解。
- 有正有负才求解。`t=(date - first_date).days / 365`，`NPV(r)=sum(cf / (1+r)^t)`。先在 `[-0.9999, 10]` 以 0.01 步长找变号区间，再在区间内二分，最多 200 次；收敛条件 `abs(NPV) < 1e-6 * sum(abs(cf))`。多个变号区间时取 `r` 最小且 `NPV` 变号的那个；找不到 → 无解。
- 输出 6 位小数比率（`0.123456` 表示 12.3456%）；无解输出 `null`，原因写在 `xirrReason`。

### 6.5 报告指标

- 持仓：所有被查账户按代码合并后 `qty > 0` 的代码；名称取最近一笔交易的名称；成本 = 各账户 `total_cost` 之和；市值 = `qty * price`（无价为空）；占比 = 市值 / 已定价总市值（分母 0 时为 null）。
- `--account` 指定时只合并该账户；不指定时合并全部账户。
- 汇总：
  - `marketValue` = 已定价持仓市值之和；
  - `totalCost` = 所有持仓 total_cost 之和；
  - `unrealizedPnl` = 已定价持仓的（市值 - 成本）之和；
  - `unrealizedPct` = `unrealizedPnl / 已定价持仓成本`；分母 0 时为 null；
  - `realizedPnl` = 所有已实现之和；
  - `dividends` = 所有分红之和；
  - `fees` = 四个费用字段总和，分项也输出；
  - `xirr` 见 6.4；
  - `priced` / `unpriced` 数量。
- 集中度：`top3`、`top5` = 按市值降序前 3/5 的占比之和；已定价总市值为 0 时为 null。
- `tradeStats.total` 是截止日前的全部交易笔数；`buy`/`sell` 只数 `buy` 和 `sell`（`ipo_allot`/`rights_issue` 不计入）；`turnover` = 买入 gross + 卖出 gross。
- 费用占成交额：`fees.total / (买入 gross + 卖出 gross)`，分母 0 为 null。

## 7. 报告

### 7.1 文本报告

顺序：
1. 头部：`持仓簿 报告`、截止日、账户数、标的数、行情来源。
2. 摘要：总市值、总成本、未实现盈亏（金额/百分比）、已实现盈亏、分红收入、费用合计、XIRR。
3. 持仓表：代码、名称、数量、成本价、现价、市值、占比、未实现盈亏；按市值降序，无价排最后。
4. 费用分项：佣金、印花税、过户费、其他；交易笔数、买入/卖出笔数、费用占成交额。
5. 数据来源：文件名 + 行数。
6. 免责声明：`本工具只做账目核对与统计，不构成投资建议；结果依赖你导入的数据是否完整。`

无价标的显示 `—`，顶部一行 `N 只标的没有价格，未计入市值与盈亏`。

### 7.2 HTML 报告

- 单文件、无外部资源、无 JavaScript；CSS 内联；UTF-8；CSP：
  `default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'`。
- 必须包含：header（数据截止日、账户数、标的数、行情来源和抓取时间）、摘要、持仓表、集中度（前 3/前 5 占比）、费用、数据来源、免责声明。不包含对账区块。
- header 的抓取时间：`--market live` 显示该次抓取的本机时间；`off`/`mock` 只显示来源，不显示时间。
- 所有 CSV/券商文本 HTML 转义；不内联用户 HTML/CSS/URL。
- 375px 无页面级横向滚动；表格在窄屏改为块列表；打印保留全部数据。
- 视觉规则见 `DESIGN.md`。

### 7.3 警告码

| 码 | 触发 |
|---|---|
| `missing_price` | 标的没有可用价格 |
| `manual_price_ignored` | `--prices` 中价格日期晚于 `as_of`，未采用 |
| `market_partial` | `--market live` 部分代码失败，使用了缓存或无价 |
| `xirr_cost_fallback` | XIRR 期末有标的按成本价替代市值 |
| `source_missing` | 有交易但找不到对应 `imports` 记录（手工改库） |

### 7.4 JSON 报告

字段固定如下；金额/数量/价格为字符串，比率为 number 或 null：

```json
{
  "ok": true,
  "asOf": "2026-10-29",
  "accounts": 2,
  "instruments": 8,
  "market": "off",
  "summary": {
    "marketValue": "270800.00",
    "totalCost": "316010.00",
    "unrealizedPnl": "-45210.00",
    "unrealizedPct": -0.1431,
    "realizedPnl": "13985.00",
    "dividends": "600.00",
    "fees": {"commission": "120.00", "stamp": "80.00", "transfer": "12.00", "other": "0.00", "total": "212.00"},
    "feesToTurnover": 0.0008,
    "xirr": 0.081234,
    "xirrReason": null,
    "priced": 7,
    "unpriced": 1
  },
  "positions": [
    {"code":"600519.SH","name":"贵州茅台","quantity":"200.0000","avgCost":"1580.0500","price":"1354.0000","marketValue":"270800.00","weight":1.0,"unrealizedPnl":"-45210.00","unrealizedPct":-0.1431}
  ],
  "top3": 1.0,
  "top5": 1.0,
  "tradeStats": {"total": 2, "buy": 1, "sell": 1, "turnover": "159480.00"},
  "sources": [{"file":"交割单_2026.csv","rows":126}],
  "warnings": [{"code":"missing_price","message":"510300.SH 没有价格，未计入市值与盈亏"}]
}
```
- `accounts` 是参与报告的账户数（有交易或持仓的账户）；`instruments` 是持仓标的数（qty>0）；`sources[].rows` 来自 `imports.rows_read`，同一文件多次导入取最后一次；指定 `--account` 时 sources 只含该账户。
- 没有价时 `price`、`marketValue`、`unrealizedPnl`、`unrealizedPct` 为 `null`。
- JSON 模式不写报告文件；`report_path` 不出现在 JSON。

## 8. 行情

### 8.1 live

- 地址：`HOLDBOOK_QUOTE_BASE_URL` 默认 `https://qt.gtimg.cn/q`；请求 `GET base + "=" + ",".join(codes)`，例如 `https://qt.gtimg.cn/q=sh600519,sz000001`。代码小写前缀 + 6 位数字。
- 批量最多 60 个代码；批次之间至少 1000 ms；单次运行总请求时间上限 30 秒；同一代码只请求一次。达到 30 秒后停止后续批次，已取到的价格照常使用；全部代码无价才退出 4。
- 超时 `HOLDBOOK_MARKET_TIMEOUT` 秒；失败重试 `HOLDBOOK_MARKET_RETRIES` 次；退避 1 秒、2 秒、4 秒（最多 5 次）；只对超时、连接错误、HTTP 429、HTTP 5xx 重试；4xx 不重试。
- 响应按 `gb18030` 解码；每行 `v_<prefix><code>="<fields>";`；字段 `~` 分隔，索引 1 名称、3 最新价、4 昨收。价格必须 >0 且 ≤1e6；名称截断到 40 字符。
- `v_pv_none_match` 或缺少对应行 → 该代码失败。
- 成功写入 `prices`，`date=as_of`，`source=live`，`fetched_at` 当前 UTC。

### 8.2 mock

- 不发网络请求；固定表：
  `600519.SH=1354.00`、`000001.SZ=11.54`、`510300.SH=4.001`、`113050.SH=144.967`、`159915.SZ=2.535`、`601318.SH=62.30`、`000858.SZ=118.75`、`512880.SH=1.234`；表外 `10.00`。
- 写入 `prices`，`date=as_of`，`source=mock`；请求计数为 0。

### 8.3 失败

- 单代码失败：用缓存价；无缓存则 `—`，stderr 警告带 HTTP 状态码或「连接超时（N 秒）」。
- 全部失败且无缓存：退出 4 `market_unavailable`，不生成报告/文件。
- 错误不含请求头、完整 URL 的 query 之外的敏感内容。

## 9. 错误码与限制

| 码 | 退出码 | 触发 |
|---|---|---|
| `bad_config` | 2 | 环境变量或参数不合法 |
| `data_dir_unwritable` | 1 | 数据目录/DB 父目录不可创建或不可写 |
| `db_corrupt` | 1 | `quick_check` 失败 |
| `conflict` | 1 或 2 | 已有非本工具库、schema 版本不支持、`reset` 缺 `--yes`、备份目标存在 |
| `file_not_found` | 1 | 输入文件不存在 |
| `file_not_writable` | 1 | 报告/备份目标不可写 |
| `bad_csv` | 2 | 编码失败、文件过大、行数超限 |
| `missing_columns` | 2 | 缺表头/必需列 |
| `duplicate_column` | 2 | 表头重复 |
| `bad_row` | 2 | 行级校验失败（其他行可入库） |
| `bad_date` | 2 | 日期非法或未来 |
| `bad_code` | 2 | 代码无法规范化或不存在 |
| `unknown_account` | 2 | 账户不存在 |
| `no_data` | 1 或 0 | 报告/explain/status 无数据；check 无数据为 0 |
| `ledger_negative` | 2 | 卖出超过持仓 |
| `market_unavailable` | 4 | 行情全部失败且无缓存 |
| `report_write_failed` | 1 | 报告写入失败 |
| `internal_error` | 1 | 未预期内部异常（不打印 traceback，除非 `--verbose`） |

限制汇总：单文件 ≤ `HOLDBOOK_MAX_IMPORT_BYTES`（默认 104857600）；导入数据行 ≤ `HOLDBOOK_MAX_IMPORT_ROWS`（默认 200000）；文本字段 ≤ 200 字符；账户名 ≤ 40；名称 ≤ 40；代码 ≤ 20；数量/价格 ≤ 1e9；金额 ≤ 1e12；行情每批 60；超时 1–60 秒；重试 0–5；SQLite 写锁 5000 ms；XIRR 200 次迭代；报告最大 20 MiB（超过 → 1 `report_write_failed`）。

## 10. 安全与隐私

- 全部数据本地；不联网，除用户显式 `--market live`。
- 报告/日志/错误不包含券商账号、密码、请求头；本工具不收集这些。
- 报告 HTML 转义所有外部文本；CSP 禁止脚本和外部资源。
- 数据库、备份、报告文件权限 `0600`；数据目录 `0700`。
- 不执行 CSV 中的公式、宏、HTML；不把字段当作路径或 URL。
- 报告只写用户指定路径或数据目录；原子写入（临时文件 + `os.replace`），失败删除临时文件。
- `remove`/`reset` 只删数据库三件套，不递归删目录。

## 11. 测试要求

全部测试用 `python3 -m unittest discover -s tests -v`，临时目录，不联网；live 用本机 `http.server` 假服务；不使用固定 `sleep`，等待条件或注入 `sleep=0`。

### 配置与库
1. 默认值、`--data-dir` 覆盖、非法环境变量（市场/超时/重试/容差/日志）→ 2 `bad_config`，错误带变量名。
2. 数据目录只读 → 1 `data_dir_unwritable`，不建库。
3. `init` 建库、schema=1、目录 0700、DB 0600；重复 init 幂等。
4. 非本工具 SQLite 文件 → 1 `conflict`，内容不变。
5. schema=2 → 1 `conflict`。
6. 损坏 DB → 1 `db_corrupt`。
7. `status` 健康库字段正确；无库 → 1 `no_data`。
8. `backup` 产物 `integrity_check=ok`；目标存在 → 2 `conflict`。
9. `reset` 无 `--yes` → 2；有 → DB/WAL/SHM 删除，其他文件保留。

### 导入
10. UTF-8、GB18030、逗号/制表符/分号各一条。
11. 表头在前 5 行内；3 种日期别名、3 种代码别名、3 种操作别名各一条。
12. 缺日期/操作列 → 2 `missing_columns`，带文件名与列名。
13. 同文件导入两次 → 第二次 added=0、skipped=N，库内不变。
14. 同文件两行完全相同 → 都入库（occurrence 0/1）。
15. 两个文件各一行相同 → 第二次 skipped。
16. 非法日期、非数字数量、数量/价格超限、未知代码各一条错误行；其余行入库；退出 2；最多显示 10 条。
17. 9 种 kind 文本各一条；`other` 计数；现金事件无代码 → CASH.CASH。
18. 买入：数量×价格与金额一致；金额不一致 → 错误行；只有金额+数量可反推价格。
19. `amount` 符号按 kind 归一；负数量买入取绝对值并警告。
20. 费用字段解析（千分位、括号负数）；`fee` 行只计一次。
21. 行数超限 → 2 `bad_csv`，0 行入库；100 MiB 用伪造 stat 或生成稀疏文件测。
22. 文件级错误导致多文件全部不提交；行级错误不阻止其他文件。
23. `--dry-run` 不写库，`wouldAdd` 正确。
24. 事务中途异常 → 回滚，笔数不变（注入抛异常的 reader）。
25. 账户自动创建；`--broker` 更新；账户名超 40 → 2。

### 快照
26. 同日重复导入覆盖并计数；qty=0 保留。
27. 未来日期 → 2 `bad_date`；缺代码列 → 2。
28. 同文件同代码重复 → 取最后一行并警告。
29. 未知账户 → 2 `unknown_account`。
30. `--dry-run` 不写库。

### 对账
31. 7 种 status 各一条精确用例。
32. 成本容差边界：差 = 容差 → matched；差 = 容差 + 0.01 → cost_off。
33. 4 条提示规则各一条（含送转数量相等）。
34. `--strict` 有差异 → 3；无差异 → 0；不加 strict → 0。
35. `--as-of` 缺快照 → 2 `no_data`，带最近日期。
36. 无数据 → 0，提示先导入。
37. 卖出超过持仓 → 2 `ledger_negative`，带账户/代码/日期。

### 账本与指标
38. 买入→部分卖出：avg cost、cost_out、realized 手算核对（含买卖费用）。
39. 分红不改成本；送转只改数量；配股增加成本；fee 只进费用。
40. 清仓后 avgCost=null，realized 累计正确。
41. 两次买入不同价：移动加权平均正确；再卖出后成本正确。
42. XIRR 单笔一年翻倍 → 1.0（误差 <1e-6）；两笔已知现金流 → 预计算值；方向单一 → null。
43. 期末无价用成本；无价标的进入 warning。
44. 集中度 top3/top5 手算；费用分项合计 = 四字段之和。
45. 费用占成交额分母 0 → null。

### 行情
46. 假服务器返回 GBK 十段格式 → 名称/价格解析，写 `prices`，source=live。
47. `v_pv_none_match` → 该代码失败，其他正常。
48. 价格 0/负/超 1e6 → 失败，不写。
49. 500/429 → 重试次数正确（注入 sleep=0）；4xx 不重试。
50. 超时 → 重试后 `market_unavailable`，退出 4。
51. 120 个代码 → 2 次请求（每批 60）。
52. mock 表内 8 个价格准确，表外 10.00，0 网络请求。
53. 部分失败用缓存；全失败无缓存 → 4。

### 报告与 CLI
54. 文本报告包含摘要、持仓、费用、来源、免责声明；无价显示 `—`。
55. HTML 合法、CSP、转义 `<script>`、无外部资源、375px 无横向滚动（静态检查 viewport/表格）。
56. `--format html` 写文件并打印路径；`--out -` 只输出 HTML；`--json` 不写文件。
57. JSON 字段固定（`sorted(keys)`）；金额为字符串；null 正确。
58. `--json` 每个命令一行合法 JSON；错误走 stdout 且退出码正确。
59. 退出码 0/1/2/3/4 各一条。
60. `explain` 文本/JSON 与重放一致；多账户无 `--account` → 2。
61. 同一命令连续跑 3 次结果一致（时间戳除外）。
62. 报告/备份原子写：注入写入失败，无半文件。

## 12. 版本与文档

- 版本 `0.1.0`：包、`--version`、README、CHANGELOG 一致。
- schema 版本 1；不兼容变更需迁移，旧库不自动升级。
- 公式版本 `holdbook-ledger-v1`，写入报告「数据与方法」。
- README、README_EN、docs/features.md、development.md、deviations.md、external-apis.md 必须与实现一致。
