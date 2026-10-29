# 持仓簿 Holdbook — 规格（v0.1.0）

个人投资者的本地持仓对账工具：把多家券商导出的交易流水合并成一本可核对的账，用当前持仓快照对账，对不上的地方定位到具体某一行，并给出真实成本、真实收益（含分红送转）和费用合计。

**本文件是唯一契约。** 命令名、参数名、字段名、枚举值、退出码、数字阈值必须一字不差地实现。有歧义按「最简单、能跑通」处理，并在 [docs/deviations.md](deviations.md) 编号记录，不准悄悄偏离。界面设计见 [DESIGN.md](DESIGN.md)；外部接口的依据与实测结果见 [docs/external-apis.md](external-apis.md)。

---

## 0. 第一版只做的一条核心流程

```
holdbook init                     建库
holdbook import 交割单.csv ...     导入（多券商、可重复导入）
holdbook snapshot 持仓.csv         录入某一天的券商持仓快照
holdbook check                    对账：账算出来的数量和成本 vs 券商快照
holdbook report --open            生成 HTML 报告（真实成本、盈亏、XIRR、费用）
```

这五步是唯一要打透的流程。跑通之后，用户每周调仓后重复 `import` + `snapshot` + `check` + `report` 即可。

### 非目标（第一版明确不做）

- 不接券商交易接口，不下单，不做自动交易。
- 不做云同步、账号体系、多人协作；数据只在本机。
- 不抓基金季报、不做基金底层穿透。
- 不做期权、期货、外汇保证金、融资融券利息的完整口径（融资标的可以导入，按普通买卖记账，利息行记为费用）。
- 不做税务申报口径（不做逐笔申报表、不做境外所得抵免）。
- 不做分钟级行情、不做盯盘、不做买卖建议。
- 不做 Web 服务、不做账号密码、不联网注册（`init` 不需要任何账号）。
- 不内置大模型，不做 AI 点评。

## 1. 目录与技术栈

```
holdbook/                     仓库根目录
  holdbook/                   Python 包（可直接 python3 -m holdbook 运行）
    __main__.py               CLI 入口
    cli.py                    参数解析、输出、退出码
    config.py                 环境变量与校验
    db.py                     SQLite 连接、迁移、事务
    models.py                 dataclass 与枚举
    importers/                csv 读取、列名别名、编码探测、券商适配
    ledger.py                 持仓重放、成本、已实现/未实现
    reconcile.py              对账算法与差异分类
    metrics.py                XIRR、集中度、费用统计
    market.py                 行情获取（live）与 mock
    report/                   HTML 与终端报告渲染
    templates/report.html     单文件模板（内联 CSS，无 JS 依赖）
  tests/                      unittest，stdlib 只有
  docs/
  SPEC.md  DESIGN.md  README.md  README_EN.md  CHANGELOG.md  LICENSE
  pyproject.toml
```

- 语言：**Python，最低 3.10**（本机实测 3.13.5）。生产代码不使用任何第三方运行依赖，只用标准库（`sqlite3`、`csv`、`json`、`urllib`、`argparse`、`hashlib`、`html`、`decimal`）。
- 安装方式（三种都必须在 README 里给出，且都可用）：
  1. 零安装：`git clone <repo> && cd holdbook && python3 -m holdbook --help`；
  2. `pipx install .`；
  3. `python3 -m pip install --user .`。
- 金额与数量计算一律用 `decimal.Decimal`，不用 float。金额保留 2 位小数，数量保留 4 位小数，成本价保留 4 位小数。舍入用 `ROUND_HALF_UP`。
- 本地时区：交易日只到「日」，不做时区换算；`--today` 默认取本机日期。
- 端口：无（纯本地命令，不监听端口）。

## 2. 环境变量

只读 `HOLDBOOK_` 前缀。**禁止读取** `LLM_API_KEY`、`OPENAI_API_KEY`、`HTTPS_PROXY` 之类通用名作为配置来源（代理按 urllib 默认行为走系统设置，不作为本项目配置）。

| 变量 | 默认 | 必填 | 非法值处理 |
|---|---|---|---|
| `HOLDBOOK_DATA_DIR` | `~/.local/share/holdbook` | 否 | 无法创建或不可写：退出码 1，`错误: 数据目录不可写: <路径>`（code=`data_dir_unwritable`） |
| `HOLDBOOK_DB_PATH` | 空（用 `$HOLDBOOK_DATA_DIR/holdbook.db`） | 否 | 父目录不存在则创建；仍不可写按上一条处理 |
| `HOLDBOOK_MARKET` | `off` | 否 | 只接受 `off` / `live` / `mock`，其他值退出码 2，`错误: HOLDBOOK_MARKET 只能是 off、live 或 mock，收到 "<值>"`（code=`bad_config`） |
| `HOLDBOOK_QUOTE_BASE_URL` | `https://qt.gtimg.cn/q` | 否 | 不是 http/https 开头：退出码 2（code=`bad_config`） |
| `HOLDBOOK_MARKET_TIMEOUT` | `8`（秒） | 否 | 不是 1–60 的整数：退出码 2（code=`bad_config`） |
| `HOLDBOOK_MARKET_RETRIES` | `2` | 否 | 不是 0–5 的整数：退出码 2（code=`bad_config`） |
| `HOLDBOOK_MAX_IMPORT_ROWS` | `200000` | 否 | 不是 1–2000000 的整数：退出码 2（code=`bad_config`） |
| `HOLDBOOK_COST_TOLERANCE_PCT` | `0.5` | 否 | 不是 0–100 的数：退出码 2（code=`bad_config`） |
| `HOLDBOOK_COST_TOLERANCE_ABS` | `1.00` | 否 | 不是 ≥0 的数：退出码 2（code=`bad_config`） |
| `HOLDBOOK_LOG_LEVEL` | `warn` | 否 | 只接受 `debug`/`info`/`warn`/`error`，其他退出码 2 |

- 配置错误时：**不建库、不动数据**，只把错误写到 stderr 后退出。
- 本工具不保存任何密钥；任何输出（终端、日志、HTML）都不包含密钥。唯一的网络请求是行情，不需要鉴权。

## 3. 数据模型（SQLite）

数据库文件 `holdbook.db`。启用 `PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`、`PRAGMA busy_timeout=5000`。

### 3.1 `meta`
| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | TEXT PRIMARY KEY | 允许值：`schema_version`、`created_at` |
| `value` | TEXT NOT NULL | `schema_version` 第一版固定 `1` |

### 3.2 `accounts`
| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT NOT NULL UNIQUE | 账户名，1–40 字符，去掉首尾空白后不能为空，不能含换行 |
| `broker` | TEXT NOT NULL DEFAULT '' | 券商名，0–40 字符 |
| `created_at` | TEXT NOT NULL | ISO 8601 UTC，例 `2026-10-29T12:00:00Z` |

### 3.3 `instruments`
| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | TEXT PRIMARY KEY | 规范代码：`600519.SH`、`000001.SZ`、`510300.SH`、`113050.SH` |
| `name` | TEXT NOT NULL DEFAULT '' | 最近一次见到的名称 |
| `board` | TEXT NOT NULL | 枚举：`sh_main`、`sz_main`、`sz_gem`、`sh_star`、`etf`、`bond_cb`、`unknown` |
| `updated_at` | TEXT NOT NULL | ISO 8601 UTC |

代码规范化规则（输入可以是 `600519`、`sh600519`、`600519.SH`、`SH600519`）：
- 6 位数字 + 前缀 `sh`/`SH` 或后缀 `.SH` → 上交所；`sz`/`SZ`/`.SZ` → 深交所。
- 只有 6 位数字时按首位判断：`5`/`6`/`9` 开头的 6 位数字 → `.SH`；`0`/`1`/`2`/`3` 开头 → `.SZ`。
- `11xxxx`、`12xxxx` → 可转债（`.SH`/`.SZ` 按上条）：`11` 开头 → `.SH`，`12` 开头 → `.SZ`。
- `51`/`56`/`58` 开头 → ETF（`.SH`）；`15`/`16`/`18` 开头 → ETF（`.SZ`）。
- `board` 判定：`60` → `sh_main`；`00`/`000` → `sz_main`；`30` → `sz_gem`；`68` → `sh_star`；ETF 代码 → `etf`；可转债 → `bond_cb`；其余 `unknown`。
- 无法规范化的代码（非 6 位数字、含无法识别的前后缀）→ 该行记为错误行，不写库。

### 3.4 `transactions`
| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `account_id` | INTEGER NOT NULL REFERENCES accounts(id) | |
| `trade_date` | TEXT NOT NULL | `YYYY-MM-DD`，真实日历日；非法日期该行为错误行 |
| `kind` | TEXT NOT NULL | 见 3.4.1 |
| `code` | TEXT NOT NULL REFERENCES instruments(code) | 现金类事件（`fee`、`transfer_in/out`、`dividend` 的现金派发）也必须有代码；纯账户级事件用代码 `CASH.CASH`（见 3.4.2） |
| `quantity` | TEXT NOT NULL | 数量（十进制字符串），买入/送转为正，卖出为负；现金事件为 `0` |
| `price` | TEXT NOT NULL | 成交价（十进制字符串），现金事件为 `0` |
| `amount` | TEXT NOT NULL | 该行的货币金额（十进制字符串，2 位小数）：买入为负（现金流出），卖出为正（现金流入），分红为正，费用为负 |
| `fee_commission` | TEXT NOT NULL DEFAULT '0.00' | 佣金 |
| `fee_stamp` | TEXT NOT NULL DEFAULT '0.00' | 印花税 |
| `fee_transfer` | TEXT NOT NULL DEFAULT '0.00' | 过户费 |
| `fee_other` | TEXT NOT NULL DEFAULT '0.00' | 规费等其他费用 |
| `note` | TEXT NOT NULL DEFAULT '' | 券商给的原始摘要，最多 200 字符 |
| `source_file` | TEXT NOT NULL | 导入时文件名（basename），最多 200 字符 |
| `source_line` | INTEGER NOT NULL | 该行在源文件中的行号（1 起，含表头） |
| `row_hash` | TEXT NOT NULL UNIQUE | 见 3.4.3 |
| `created_at` | TEXT NOT NULL | ISO 8601 UTC |

索引：`(account_id, trade_date, id)`、`(code)`。

#### 3.4.1 `kind` 枚举与识别规则

原始「操作/摘要」文本（去空白、转小写）包含下列任一词即判定，按下列顺序**从上到下**匹配，先命中先用：

| 顺序 | 匹配词 | kind |
|---|---|---|
| 1 | `红利`、`股息`、`分红`、`dividend` | `dividend` |
| 2 | `红股`、`送股`、`转增`、`送转`、`bonus` | `bonus_share` |
| 3 | `配股`、`rights` | `rights_issue` |
| 4 | `申购`、`中签`、`新股` | `ipo_allot` |
| 5 | `利息`、`费用`、`手续费`、`佣金`、`规费` | `fee` |
| 6 | `银证转入`、`银行转入`、`转入` | `transfer_in` |
| 7 | `银证转出`、`银行转出`、`转出` | `transfer_out` |
| 8 | `买入`、`证券买入`、`buy` | `buy` |
| 9 | `卖出`、`证券卖出`、`sell` | `sell` |

- 都没命中 → `other`，该行**照常入库**（便于用户看到），并在导入摘要里计为「未识别 1 行」，同时打印 `警告: 第 N 行未识别的操作「<原文>」，已按 other 入库，不计入持仓`。
- `dividend`：`quantity` 为送股数量时归 `bonus_share`；现金分红 `quantity=0`、`amount` 取该行金额的绝对值。
- `bonus_share`：`quantity` 取该行数量绝对值，`amount=0`，`price=0`。
- `rights_issue`：`quantity` 取正，`amount` 为负（现金流出），`price` = 金额/数量（保留 4 位）。
- `ipo_allot`：按 `buy` 处理（计入持仓与成本），但 `kind` 保留 `ipo_allot` 以便区分。
- `fee`：`amount` 取负，且 `fee_other` 记同额绝对值；该行**不参与**持仓重放的成本，只进费用合计。
- `transfer_in`/`transfer_out`：只影响现金，不影响持仓；`amount` 正/负。
- `other`：不入持仓账，只进「未识别」统计。

#### 3.4.2 现金类代码
- 账户级现金事件（`transfer_in`、`transfer_out`、账户级的 `fee`）使用代码 `CASH.CASH`，`board` 记 `unknown`，`name` 记「现金」。
- 无法解析出代码、但 kind 是 `transfer_in`/`transfer_out`/`fee` 的行，自动用 `CASH.CASH`。

#### 3.4.3 去重键 `row_hash`
```
row_hash = sha256(account_name + "\x1f" + trade_date + "\x1f" + kind + "\x1f" + code
                  + "\x1f" + quantity + "\x1f" + price + "\x1f" + amount
                  + "\x1f" + 四个费用 + "\x1f" + 同文件内相同内容的出现序号)
```
- 「出现序号」从 0 开始，同一文件内上述字段完全相同的第 2 行序号为 1，以此类推；序号只在一次导入的文件内计算。
- 效果：同一个文件重复导入 0 行新增（幂等）；同一天同价同量的两笔真实成交（成交编号不同但我们读不到）不会互相吞掉。
- `note`、`source_file`、`source_line` 不参与 hash。

### 3.5 `snapshots`
| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `account_id` | INTEGER NOT NULL REFERENCES accounts(id) | |
| `as_of_date` | TEXT NOT NULL | `YYYY-MM-DD` |
| `code` | TEXT NOT NULL REFERENCES instruments(code) | |
| `quantity` | TEXT NOT NULL | 券商显示的数量（十进制字符串） |
| `cost_price` | TEXT | 券商显示的成本价，`NULL` 表示快照里没有这一列 |
| `market_value` | TEXT | 券商显示的市值，`NULL` 表示没有 |
| `source_file` | TEXT NOT NULL | |
| `created_at` | TEXT NOT NULL | |
| UNIQUE | | `(account_id, as_of_date, code)`；同账户同日期重复导入 = 覆盖该代码的值（不是报错），并打印 `提示: 已覆盖 2026-10-29 的 3 条快照` |

### 3.6 `prices`
| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | TEXT NOT NULL | |
| `date` | TEXT NOT NULL | `YYYY-MM-DD` |
| `close` | TEXT NOT NULL | 收盘价/最新价，十进制字符串 |
| `source` | TEXT NOT NULL | 枚举：`live`、`mock`、`manual` |
| `fetched_at` | TEXT NOT NULL | ISO 8601 UTC |
| PRIMARY KEY | | `(code, date)` |

## 4. 命令行契约

所有命令通用参数：

| 参数 | 说明 |
|---|---|
| `--data-dir PATH` | 覆盖 `HOLDBOOK_DATA_DIR`（优先级最高） |
| `--json` | 输出单行 JSON 到 stdout，字段见各命令；此时人类可读文本不输出 |
| `--today YYYY-MM-DD` | 覆盖「今天」（用于报告截止日、默认快照日），非法日期退出码 2 |
| `--quiet` | 不打印进度与提示，只打印结果与错误 |
| `-v/--verbose` | 等价于 `HOLDBOOK_LOG_LEVEL=info` |

**退出码（全局约定）**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行时错误（数据目录不可写、数据库损坏、文件不存在、行情请求失败等） |
| 2 | 输入/配置错误（参数非法、CSV 表头缺列、代码非法、日期非法、环境变量非法） |
| 3 | `check` 发现对账差异（仅在 `--strict` 时；不加 `--strict` 时差异也算成功，返回 0） |
| 4 | 行情不可用（`report --market live` 请求失败，且没有可用缓存） |

**错误输出格式**：stderr 一行 `错误: <中文说明> (code=<机器码>)`；`--json` 时 stdout 输出 `{"error":"<中文说明>","code":"<机器码>"}`。机器码首版至少包含：`bad_config`、`data_dir_unwritable`、`db_corrupt`、`file_not_found`、`bad_csv`、`missing_columns`、`bad_row`、`bad_date`、`bad_code`、`unknown_account`、`no_data`、`market_failed`、`market_unavailable`、`conflict`。

### 4.1 `holdbook init`
- 参数：无（`--data-dir` 可用）。
- 行为：创建数据目录（`0o700`）、建库、写 `meta`。已存在则不动数据，打印 `已就绪: <db 路径>`。
- 输出（文本）：
  ```
  数据目录: /Users/x/.local/share/holdbook
  数据库: /Users/x/.local/share/holdbook/holdbook.db
  已就绪（schema_version=1）
  ```
- `--json`：`{"ok":true,"dbPath":"...","schemaVersion":1}`。
- 失败：目录不可写 → 退出码 1 `data_dir_unwritable`；已存在但不是本工具的库（缺 `meta.schema_version`）→ 退出码 1 `conflict`，提示路径，不覆盖。

### 4.2 `holdbook import FILE... --account NAME [--broker NAME] [--encoding auto|utf-8|gb18030] [--dry-run]`
- `--account` 必填；账户不存在则自动创建（打印 `提示: 新建账户「<名字>」`）。
- 支持一次传多个文件，按参数顺序处理；同一文件内按行处理。
- 编码：默认 `auto`，先按 UTF-8 严格解码，失败则用 `gb18030`；`--encoding` 强制指定。解码失败（替换字符比例 > 0）→ 退出码 2 `bad_csv`。
- 分隔符：逗号、制表符、分号自动探测（取表头行里最多的那种）；都不出现则按逗号。
- 表头识别：第一行含「日期」类列名即视为表头；前 5 行内找表头，找不到 → 退出码 2 `missing_columns`。
- 列名别名（去空格、全角转半角、小写后比较）：
  - 日期：`成交日期`、`发生日期`、`交易日期`、`日期`、`交收日期`
  - 代码：`证券代码`、`股票代码`、`代码`、`symbol`、`code`
  - 名称：`证券名称`、`股票名称`、`名称`、`name`
  - 操作：`操作`、`买卖标志`、`业务名称`、`摘要`、`交易类型`、`business`
  - 数量：`成交数量`、`发生数量`、`数量`、`qty`、`quantity`
  - 价格：`成交价格`、`成交均价`、`价格`、`price`
  - 金额：`成交金额`、`发生金额`、`金额`、`amount`
  - 费用：`手续费`、`佣金`、`commission`；`印花税`、`tax`；`过户费`、`transferfee`；`其他费用`、`规费`、`otherfee`
- **必需列**：日期、操作。缺任一项 → 退出码 2 `missing_columns`，错误里列出缺的列名与文件名。
- 其余列缺失时按「空」处理；`数量`/`价格`/`金额` 全缺且 kind 不是现金类 → 该行为错误行。
- 逐行校验（任一失败 → 该行为错误行，跳过并继续）：
  - 日期必须能解析为 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYYMMDD`、`YYYY-MM-DD HH:MM:SS` 之一；
  - 数量、价格、金额、费用必须是数字（允许千分位逗号、全角数字、前导 `+`、括号负数 `(1,234.50)`）；`NaN`/`Inf` 不接受；
  - 数量绝对值 > 1e9 或价格 > 1e9 → 错误行；
  - 代码无法规范化且 kind 不是现金类 → 错误行。
- 行数上限 `HOLDBOOK_MAX_IMPORT_ROWS`（按单个文件计），超出 → 退出码 2 `bad_csv`，不写入任何数据。
- 全部写入在**一个事务**里：解析完才提交；中途异常回滚，库保持导入前状态。
- 输出（文本）：
  ```
  账户: 华泰
  文件: 交割单_2026.csv
  读取 128 行，表头在第 1 行
  新增 126 笔，跳过重复 2 笔，错误 0 行
  按类型: buy 72, sell 40, dividend 6, bonus_share 3, fee 5
  未识别: 0 行
  ```
  多个文件时每个文件一段，最后一行汇总：`合计: 新增 N 笔，跳过 M 笔，错误 K 行`。
- 有错误行时额外打印最多 10 条明细：`错误: 第 37 行：数量不是数字「-」 (code=bad_row)`，其余折叠为 `还有 N 行错误未显示`。**错误行不阻止其他行入库**，但只要存在错误行，退出码为 2（数据仍已提交，摘要里写明 `已入库，但有 K 行未导入`）。
- `--dry-run`：完整解析与校验，输出同一份摘要并把 `新增` 改为 `将新增`，不写库，退出码同上。
- `--json`：`{"files":[{"file":"...","rows":128,"added":126,"skipped":2,"errors":[{"line":37,"message":"数量不是数字「-」"}]}],"totals":{"added":126,"skipped":2,"errors":0},"accountsCreated":["华泰"]}`。
- 失败：文件不存在 → 1 `file_not_found`；同一行重复出现在两个文件里 → 第二次算跳过（幂等）。

### 4.3 `holdbook snapshot FILE --account NAME [--as-of YYYY-MM-DD] [--dry-run]`
- 快照 CSV 列别名：
  - 代码：同 4.2；名称：同 4.2；数量：`持仓数量`、`股票余额`、`当前持仓`、`数量`、`qty`；成本价：`成本价`、`参考成本价`、`持仓成本`、`costprice`；市值：`市值`、`参考市值`、`marketvalue`。
  - 必需列：代码、数量。
- `--as-of` 默认 `--today`；未来日期 → 退出码 2 `bad_date`。
- 数量为 0 的行**照常写入**（表示已清仓），不跳过。
- 输出：`快照: 华泰 2026-10-29，8 只标的`，覆盖时追加 `提示: 已覆盖 3 条`。
- `--json`：`{"ok":true,"account":"华泰","asOf":"2026-10-29","rows":8,"overwritten":3,"errors":[]}`。
- 同一代码在一个文件里出现两次 → 后者覆盖前者并打印警告 `警告: 代码 600519.SH 在快照文件里出现 2 次，取最后一行`。

### 4.4 `holdbook check [--account NAME] [--as-of YYYY-MM-DD] [--strict]`
- 默认对每个账户使用**该账户最新的快照**；`--as-of` 指定日期时用该日期的快照（没有则退出码 2 `no_data`，错误信息列出该账户最近一次快照日期）。
- 计算：对每个 `(account, code)`，重放 `trade_date <= as_of` 的交易得出 `qty_calc` 与 `avg_cost_calc`；与快照的 `quantity`、`cost_price` 比较。
- 差异分类（`status` 枚举）：

| status | 判定 | 说明文案（中文，直接展示） |
|---|---|---|
| `matched` | 数量相等、快照有成本价，且成本差 ≤ 容差 | 一致 |
| `qty_missing` | `qty_calc < qty_snap` | 账上少 <差值> 股：可能漏导入买入或送转 |
| `qty_extra` | `qty_calc > qty_snap` | 账上多 <差值> 股：可能漏导入卖出或转出 |
| `cost_off` | 数量相等，成本价差 > max(`HOLDBOOK_COST_TOLERANCE_ABS`, `cost_snap × HOLDBOOK_COST_TOLERANCE_PCT/100`) | 成本价不一致：差 <差值> |
| `only_snapshot` | 快照有、账上没有交易 | 快照有、账上没有：可能漏导入该标的全部交易 |
| `only_ledger` | 账上有数量、快照里没有该代码 | 账上有、快照没有：可能已清仓但没导入卖出 |
| `no_cost` | 数量相等但快照没有成本价 | 数量一致，券商未提供成本价，未比对成本 |

- 每个差异附带 `hints`（0–2 条，按下列固定规则生成，可为空）：
  1. 若该标的在 `as_of` 前 90 天内存在 `bonus_share` 或 `dividend` 记录 → `近期有分红送转，确认是否漏导入除权日之后的行`；
  2. 若 `qty_missing` 且差值等于该标的最近一次 `bonus_share.quantity` → `差值正好等于 <日期> 送转的 <数量> 股，可能漏导入该送转行`；
  3. 若同一 `row_hash` 曾被跳过 >0 次（本次运行内无法判断，改用：该账户存在 `source_file` 相同的多个导入批次）→ `同一来源文件导入过多次，检查是否重复导入`；
  4. 若 `only_ledger` 且该标的最后一条交易早于 `as_of` 30 天以上 → `该标的最后交易在 <日期>，可能已卖出但未导入`。
- 输出（文本）：
  ```
  对账: 华泰 @ 2026-10-29（快照来源 持仓_20261029.csv）
  标的 12 只，一致 9 只，差异 3 只

  600519.SH 贵州茅台   账上 200 股 / 快照 200 股  一致
  000001.SZ 平安银行   账上 1,000 股 / 快照 1,300 股  账上少 300 股：可能漏导入买入或送转
                         提示: 差值正好等于 2026-06-12 送转的 300 股，可能漏导入该送转行
  510300.SH 沪深300ETF 账上 5,000 股 / 快照 5,000 股  成本价不一致：差 0.0812
  ```
- 汇总行后若存在差异：`结论: 有 3 只对不上，先按上面提示逐条核对。`
- `--strict`：有任一差异 → 退出码 3；无差异 → 0。
- `--json`：`{"asOf":"...","accounts":[{"account":"华泰","snapshot":"...","items":[{"code","name","qtyCalc","qtySnapshot","avgCostCalc","costSnapshot","status","qtyDiff","costDiff","hints":[]}],"matched":9,"differed":3}],"differed":3,"strict":false}`。
- 失败：完全没有数据（无账户或无快照）→ 0，打印 `还没有数据：先运行 holdbook import 和 holdbook snapshot`（不算错误）。

### 4.5 `holdbook report [--account NAME] [--as-of YYYY-MM-DD] [--market off|live|mock] [--prices FILE] [--out PATH] [--open] [--format html|text]`
- `--market` 默认取 `HOLDBOOK_MARKET`。
- 数据截止：`--as-of` 默认 `--today`。
- 估值顺序：
  1. `--prices FILE` 提供的价格（`code,date,close` 或 `code,close`），source=`manual`，优先级最高；
  2. `prices` 表里 `date <= as_of` 的最新一条；
  3. `--market live` 时请求行情（见第 6 节），成功则写入 `prices`（source=`live`）；
  4. `--market mock` 时用 mock 价（见 6.3），source=`mock`；
  5. 都没有 → 该标的现价为空：市值、未实现盈亏、占比显示 `—`，并在报告顶部提示 `5 只标的没有价格，未计入市值与盈亏`。
- 输出：
  - `--format text`（默认）：终端表格 + 摘要；
  - `--format html`：生成单文件 HTML 到 `--out`（默认 `holdbook-report-<as_of>.html` 于当前目录），打印 `报告: /abs/path.html`；`--open` 时用系统默认程序打开（macOS `open`、Linux `xdg-open`、Windows `start`），打不开只警告，不影响退出码。
- 报告内容（HTML 与 text 一致的数据口径）：
  1. 头部：数据截止日、行情来源、账户数、标的数、口径说明（成本法=移动加权平均；未实现盈亏=市值−成本；XIRR 按现金流日期年化）；
  2. 摘要：总市值、总成本、未实现盈亏（金额与百分比）、已实现盈亏、分红收入、累计费用（佣金/印花税/过户费/其他分项）、XIRR；
  3. 持仓表：代码、名称、数量、成本价、现价、市值、占比、未实现盈亏；按市值降序；
  4. 费用与交易统计：交易笔数、买入/卖出笔数、费用合计、费用占成交额比例；
  5. 数据来源：列出用到的每个导入文件名与行数；
  6. 免责声明：`本工具只做账目核对与统计，不构成投资建议；结果依赖你导入的数据是否完整。`
- XIRR 计算（`metrics.xirr`）：
  - 现金流：每笔 `buy`/`ipo_allot`/`rights_issue` 为负的 `amount`（含费），`sell` 为正向净额（`amount` 减费用），`dividend` 为正，`transfer_in/out` 不计入，`fee` 计入负向；
  - 期末价值：`as_of` 当日全部持仓市值（无价格则用成本价替代，并在报告里注明 `XIRR 含按成本价估值的标的`）；
  - 用二分法求解 `NPV(r)=0`，区间 `[-0.9999, 10]`，迭代上限 200，精度 `1e-9`；无解或现金流方向单一 → `XIRR` 显示 `—`，并给出原因 `现金流不足，无法计算`。
- `--json`：见 5.4 的字段集合。
- 失败：无数据 → 1 `no_data`；`--market live` 且全部请求失败且无缓存 → 4 `market_unavailable`；`--out` 的父目录不存在或不可写 → 1 `file_not_writable`（错误信息带目标绝对路径，且不写任何文件）；`--out` 已存在且是目录 → 1 `file_not_writable`。

### 4.6 `holdbook explain CODE [--account NAME] [--as-of YYYY-MM-DD]`
- 打印某只标的的完整账：按时间正序列出每笔交易对数量与成本的影响，最后给出重放结果。
- 输出（文本）：
  ```
  600519.SH 贵州茅台（华泰）
  日期        类型          数量      价格       金额        费用     数量余额   成本价
  2025-03-10  buy           100       1,580.00  -158,005.00  5.00     100        1,580.05
  2025-06-20  dividend      0         0         600.00       0.00     100        1,580.05
  2025-09-02  sell          -100      1,720.00  171,995.00   5.00     0          —
  重放结果: 数量 0，平均成本 —，已实现盈亏 13,985.00
  ```
- `--json`：`{"code","name","account","rows":[{"date","kind","quantity","price","amount","fees","qtyAfter","avgCostAfter"}],"final":{"quantity","avgCost","realizedPnl"}}`。
- 失败：代码不存在 → 2 `bad_code`；无交易 → 1 `no_data`。

### 4.7 `holdbook status`
- 打印：库路径、schema 版本、账户数与各账户的交易笔数/快照日期、标的数、最后导入时间、行情模式。
- `--json`：`{"dbPath","schemaVersion","market","accounts":[{"name","transactions","lastImportAt","lastSnapshotDate"}],"instruments"}`。
- 失败：无库 → 1 `no_data`，提示先 `init`。

### 4.8 `holdbook backup [--out PATH]`
- 用 SQLite `VACUUM INTO` 生成一致副本；默认 `holdbook-backup-YYYYMMDD-HHMMSS.db`。
- 输出：`备份: /abs/path.db（<size> 字节）`。`--json`：`{"ok":true,"path":"...","bytes":123456}`。
- 失败：`--out` 的父目录不存在或不可写、或 `--out` 指向已存在的目录 → 1 `file_not_writable`（错误信息带目标绝对路径，不产生半个文件）。

### 4.9 `holdbook reset --yes`
- 删除数据库文件（只删 `holdbook.db`、`holdbook.db-wal`、`holdbook.db-shm`），保留目录。
- 没有 `--yes` → 退出码 2，`错误: 这会删除全部本地数据，确认请加 --yes (code=conflict)`。
- 输出：`已删除: <db 路径>`。

## 5. 核心流程（数字口径）

### 5.1 导入
输入：CSV 文件 + 账户名；输出：`transactions` 行 + 导入摘要。超时/重试：无（纯本地）。单文件行数上限 200000；单文件大小上限 100 MB（超出退出码 2 `bad_csv`）。整个导入在一个事务内，SQLite `busy_timeout=5000` ms。

### 5.2 快照
输入：CSV + 账户 + 日期；输出：`snapshots` 行。覆盖语义见 3.5。无重试。

### 5.3 对账
输入：账 + 快照；输出：差异清单。成本容差：`max(1.00 元, 快照成本价 × 0.5%)`。

比对范围是**账户与代码的并集**：遍历「该账户在 `as_of` 有快照行的代码」∪「该账户在 `as_of` 前有交易且重放后 `qty_calc ≠ 0` 的代码」，两端任一出现的都出一条结果，判定按 §4.4 的 7 种 status：
- 只在账上出现（`qty_calc > 0`、快照无该代码）→ `only_ledger`；
- 只在快照出现（快照有该代码、账上无任何交易）→ `only_snapshot`；
- 两边都有 → 按数量与成本比较，得出 `matched` / `qty_missing` / `qty_extra` / `cost_off` / `no_cost`；
- 账上重放后为 0 且快照也是 0 → `matched`；
- 账上重放后为 0、快照数量 > 0 → `qty_missing`（差值为快照数量）；
- 账上重放后 > 0、快照数量为 0 → `qty_extra`。

快照里完全没有该代码、账上重放后也为 0 的标的**不出现**在结果里。

### 5.4 报告
输入：账 + 价格（可选）；输出：text/HTML + JSON。不写库，除行情结果外。

## 6. 行情（唯一的外部请求）

### 6.1 live 模式
- 地址：`HOLDBOOK_QUOTE_BASE_URL`（默认 `https://qt.gtimg.cn/q`），请求方法 `GET`，query `q=<code1>,<code2>,...`。
- 代码写法：`sh600519`/`sz000001`（小写交易所前缀 + 6 位数字），由规范代码转换。
- 响应编码 GBK（`content-type: text/html; charset=GBK`），按 GBK 解码；解码失败按单条失败处理。
- 响应格式：每行 `v_sh600519="1~贵州茅台~600519~1354.00~1330.75~...";`，以 `~` 分隔；索引 1 为名称，3 为最新价，4 为昨收。未知代码返回 `v_pv_none_match="1";`。
- 批量：每次最多 60 个代码；两次请求之间至少间隔 1000 ms；总时长上限 30 秒。
- 超时：`HOLDBOOK_MARKET_TIMEOUT` 秒；失败（超时、非 200、解析失败、价格非正数）重试 `HOLDBOOK_MARKET_RETRIES` 次，退避 1 秒、2 秒（第 n 次重试等 2^(n-1) 秒）。
- 网络错误的重试只针对超时、连接错误、HTTP 5xx、HTTP 429；HTTP 4xx 不重试。
- 单次运行里同一代码只请求一次。

### 6.2 mock 模式（确定性，测试与演示用）
- `--market mock` 或 `HOLDBOOK_MARKET=mock` 时不发任何网络请求。
- 价格规则（写死在 `market.py` 的常量表里，测试直接断言）：
  - 表内代码（`600519.SH`=1354.00、`000001.SZ`=11.54、`510300.SH`=4.001、`113050.SH`=144.967、`159915.SZ`=2.535、`601318.SH`=62.30、`000858.SZ`=118.75、`512880.SH`=1.234）→ 返回表内价格；
  - 表外代码 → 返回 `10.00`；
  - 任何请求计数、耗时都为 0；`source=mock`。
- mock 下 `prices` 表写入 `date = as_of`。

### 6.3 失败的用户可见行为
- live 请求失败且该代码没有任何缓存价 → 该标的现价为空，按 4.5 的规则显示 `—`，stderr 打印 `警告: 600519.SH 行情获取失败：HTTP 502（重试 2 次后仍失败）`。
- 全部代码都失败且无缓存 → 退出码 4 `market_unavailable`，报告不生成。
- 错误信息必须带 HTTP 状态码或「连接超时（8 秒）」，不带任何请求头内容（本工具没有密钥）。

## 7. 风险与防护

| 风险 | 防护 | 测试 |
|---|---|---|
| 导入把已有数据写坏 | 单事务提交；解析全部完成才 commit；异常回滚 | `test_import_rollback_on_midway_error` |
| 重复导入造成翻倍 | `row_hash` 唯一约束 + 跳过计数 | `test_import_idempotent`、`test_duplicate_identical_rows_in_one_file` |
| 用户误删数据 | `reset` 必须 `--yes`；`backup` 生成一致副本 | `test_reset_requires_yes`、`test_backup_creates_readable_db` |
| 行情接口被替换/投毒（改一句 baseURL 就读到脏数据） | 只解析固定字段；价格必须 >0 且 ≤1e6；名称最长 40 字符；单次最多 60 个代码 | `test_market_rejects_non_positive_price`、`test_market_parses_fields` |
| 数字精度导致账算错 | 全流程 `Decimal`，禁止 float；金额 2 位、数量/成本价 4 位 | `test_decimal_no_float_drift` |
| 时区/日期导致漏交易 | 交易日按本机日期字符串比较（`YYYY-MM-DD` 字典序即时间序）；拒绝未来快照日期 | `test_snapshot_rejects_future_date` |
| 用户把成本口径误解成投资建议 | 报告与 README 写明口径与免责声明；不输出「买/卖」建议 | `test_report_contains_disclaimer` |
| 崩溃后库损坏 | WAL + `foreign_keys=ON` + 启动时 `PRAGMA quick_check`；失败退出码 1 `db_corrupt` 并提示用 `backup` 备份后重建 | `test_corrupt_db_detected` |
| 大文件拖死进程 | 行数与大小上限；逐行流式读取，不整体载入内存 | `test_import_rejects_too_many_rows`、`test_import_rejects_oversize_file` |
| 密钥泄露 | 无密钥可泄露；错误信息只含状态码与 URL 主机名 | `test_error_has_no_credentials` |

## 8. 测试要求（`unittest`，全部离线、用临时目录）

测试全部在 `tests/` 下，用 `python3 -m unittest discover -s tests -v` 跑；不联网（`HOLDBOOK_MARKET` 默认 `off`；live 相关测试用本地假行情服务器）；用 `tempfile.mkdtemp()` 做数据目录；断言实际输出与退出码。

**配置**
1. 默认值正确；`HOLDBOOK_MARKET=bogus`、超时 `abc`、重试 `-1`、容差非数字 → 退出码 2 且错误里带变量名。
2. 数据目录不可写（`chmod 0500`）→ 退出码 1 `data_dir_unwritable`，且不创建库。
3. 不读取通用名环境变量（设置 `LLM_API_KEY`、`OPENAI_API_KEY`、`HOLDBOOK_TEST_X` 不影响任何行为）。

**init / status / backup / reset**
4. 首次 `init` 建库、`meta.schema_version=1`、目录权限 `0o700`；重复 `init` 幂等。
5. `init` 指向一个已有非本工具 SQLite 文件 → `conflict`，不覆盖。
6. `status` 输出账户、笔数、快照日期、行情模式；无库时 `no_data`。
7. `backup` 产物能被 `sqlite3` 打开且 `integrity_check=ok`。
8. `reset` 无 `--yes` → 退出码 2；有 `--yes` → 文件消失、目录保留。

**导入（核心，失败分支必须覆盖）**
9. UTF-8 与 GB18030 两种编码、逗号/制表符分隔都能读。
10. 列名别名：至少 3 种不同表头写法映射到同一字段。
11. 缺「日期」列 → 2 `missing_columns`，错误里带文件名与列名。
12. 同一文件导入两次 → 第二次 `added=0`、`skipped=N`，库内笔数不变。
13. 同一天同价同量的两笔真实成交（两行内容相同）→ 都入库，不互相吞。
14. 非法日期、非数字数量、数量超上限、未知代码 → 各记为错误行；其余行照常入库；退出码 2；错误行最多显示 10 条。
15. `kind` 识别：9 种操作文本各一条用例（含中文与英文），未识别 → `other` 且计数。
16. 现金事件无代码 → 归 `CASH.CASH`，不参与持仓。
17. 超过 `HOLDBOOK_MAX_IMPORT_ROWS` 行 → 2 `bad_csv`，0 行入库。
18. 超过 100 MB → 2 `bad_csv`（用 `--dry-run` 与伪造大小，不写 100MB 文件）。
19. 中途异常（注入一个在第 50 行抛异常的读取器）→ 库回滚，笔数不变。
20. `--dry-run` 不写库但摘要与真实导入一致。
21. 多文件一次导入 → 汇总正确；部分文件失败时其他文件的数据不提交（全有或全无）。

**快照**
22. 同日同账户重复导入 → 覆盖并计数；数量为 0 的行保留。
23. 未来日期 → 2 `bad_date`；缺代码列 → 2 `missing_columns`。
24. 同一文件内同代码重复 → 取最后一行并警告。

**对账**
25. 7 种 `status` 各一条用例，构造精确数据。
26. 成本容差边界：差值恰好等于容差 → `matched`；容差 +0.01 → `cost_off`。
27. 提示规则 1–4 各一条（含「差值等于送转数量」）。
28. `--strict` 有差异 → 3；无差异 → 0；不带 `--strict` → 0。
29. `--as-of` 指定日期用对应快照；没有快照 → 2 `no_data`，错误里带最近快照日期。
30. 无任何数据 → 0 且提示先导入。

**账本与指标**
31. 买入→卖出（部分卖出）的移动加权成本、已实现盈亏逐笔核对（含费用）。
32. 分红不改成本、送转只改数量、配股增加成本与数量、`fee` 只进费用。
33. 清仓后成本价为 `—`，已实现盈亏累计正确。
34. XIRR：已知答案的两组现金流（单笔买入一年后翻倍 → 100%；两笔不同日期 → 与预计算值误差 <1e-6）；现金流方向单一 → `—` 且原因正确。
35. 集中度：前三/前五占比与总市值计算。
36. 费用分项合计 = 各笔费用之和（含 `fee` 行）。

**行情**
37. 本地假行情服务器（`http.server`）返回 GBK 的十段格式 → 解析出名称与价格；写入 `prices`（`source=live`）。
38. `v_pv_none_match="1";` → 该代码无价格，其他代码正常。
39. 价格 0 或负数 → 视为失败，不写入。
40. 假服务器返回 500/429 → 重试次数正确（用计数断言）、退避用注入的 sleep 函数（测试里注入 0，不真的等）。
41. 超时（假服务器 sleep 超过 timeout）→ 重试后 `market_unavailable`，退出码 4。
42. 批量 60 上限：120 个代码拆成 2 次请求（断言请求次数）。
43. mock 模式：表内 8 个代码价格准确，表外为 10.00，0 次网络请求（用会抛异常的假 URL 证明没请求）。

**报告**
44. HTML 生成：文件存在、包含免责声明、包含 5 个摘要字段、持仓行数与账一致。
45. text 报告：无价格标的显示 `—`，顶部有「N 只标的没有价格」。
46. `--json` 的字段集合固定（`sorted(keys)` 断言），数字为字符串或数字类型符合 5.4 约定。
47. 无数据 → 1 `no_data`。

**CLI 与输出约定**
48. 每个命令的 `--json` 输出是单行合法 JSON。
49. 退出码矩阵：0/1/2/3/4 各一条。
50. 错误信息里不含文件系统绝对路径以外的敏感内容（无密钥、无请求头）。

## 9. 版本与兼容
- `schema_version=1`。启动时若 `schema_version` 大于当前代码支持的值 → 退出码 1，`错误: 数据库版本 2 高于本程序支持的 1，请升级 (code=conflict)`。
- 数据库升级靠 `meta.schema_version` 顺序迁移，第一版只有建库。
