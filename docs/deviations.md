# 实现偏离记录

格式：编号 / SPEC 章节 / 规格 / 实际实现 / 原因 / 测试。没有偏离时不编造；每一条都能在代码或测试里找到落点。

1. **SPEC 3.4.1 / 利息**  
   规格最初把“利息”归入 `fee`。实现新增 `interest`：金额为正记利息收入，金额为负才按 `fee`。  
   原因：融资利息是支出，但保证金/债券利息是收入，强制记负会让费用和 XIRR 同时错。测试：`CsvTests.test_fee_and_interest`、`ReportTests.test_data_and_json`。

2. **SPEC 3.5 / 快照名称**  
   规格 4.3 有“名称”别名，但 3.5 表没有 `name` 列。实现给 `snapshots` 加 `name TEXT NOT NULL DEFAULT ''`，并更新 `instruments.name`（最后写入者胜）。  
   原因：否则对账展示没有名称来源。测试：`CsvTests.test_snapshot_no_date`、`CliTests.test_snapshot_check_strict`。

3. **SPEC 5.11 / demo**  
   规格新增 `demo` 命令。实现固定写 `<dir>/data`、示例 CSV 和 `report.html`，行情固定 `mock`，不联网；示例快照故意少 10 股。  
   原因：第一次使用需要一个不依赖真实 CSV 的可运行样本。测试：`CliTests.test_demo_backup_reset`。

4. **SPEC 5 全局参数 / `--json` 位置**  
   规格写全局参数在子命令前。实现额外允许 `--json`/`--quiet` 出现在任意位置（入口先摘除再交给 argparse）。  
   原因：用户常把 `--json` 写在子命令后；两种写法行为一致，不改变契约。测试：`CliTests.test_import_json_and_dry_run`。

5. **SPEC 7.4 / 比率精度**  
   规格示例只给 4 位/6 位，未写舍入。实现 `unrealizedPct`、`weight`、`top3/top5` 四舍五入 4 位；`feesToTurnover` 6 位；XIRR 6 位。  
   原因：避免 JSON 出现 17 位浮点尾巴，便于快照测试。测试：`ReportTests.test_data_and_json`。

6. **SPEC 7.4 / `marketFetchedAt` 与 `method`**  
   实现新增两个字段：`marketFetchedAt`（仅本次 live 请求写入的 UTC 最大值，否则 null）和 `method="holdbook-ledger-v1"`。  
   原因：报告要能说明“行情什么时候抓的、公式是哪版”。测试：`ReportTests.test_data_and_json`。

7. **SPEC 3.6/4.4 / 手工价自动建标的**  
   规格说 `prices.code` 外键到 `instruments`，但没说 `--prices` 遇到未知代码怎么办。实现自动建 `instruments`（名称用代码，板块按代码推断），不跳过。  
   原因：允许先导价格后导交易，且避免外键失败。测试：`ReportTests.test_manual_future_ignored`。

8. **SPEC 5.4 / 默认对账跳过无快照账户**  
   实现：默认模式某账户没有快照 → 跳过并在 stderr 警告；全部没有 → 空结果退出 0。`--as-of` 仍严格要求每个账户都有该日快照，缺 → 2 `no_data`。  
   原因：多账户场景下，一个账户还没拍快照不应该让整个 check 失败。测试：`CliTests.test_check_as_of_missing`。

9. **SPEC 5.7 / `status.instruments`**  
   实现统计 `instruments` 表全部行，包含费用/利息用的 `CASH.CASH`；报告里的 `instruments` 只数持仓。  
   原因：status 是库状态，不是持仓报告；两者口径不同，已在字段注释和 README 区分。测试：`CliTests.test_explain_and_status`。

10. **SPEC 5.2 / dry-run 的 `accountsCreated`**  
    实现 `--dry-run` 时仍会把“将要创建”的账户名放进 `accountsCreated`，但不写库。  
    原因：让用户看到真实导入会做什么；`added` 仍为 0，`wouldAdd` 给将写入数。测试：`CliTests.test_import_json_and_dry_run`。

11. **SPEC 7.3 / XIRR 成本替代警告**  
    期末有无价标的时，XIRR 用成本价替代市值，实现额外发 `xirr_cost_fallback` 警告；报告文本也追加一句。  
    原因：XIRR 的数字必须能解释，不能悄悄用成本。测试：`ReportTests.test_no_price`。

12. **SPEC 7.2 / 合计行占比**  
    HTML 合计行占比用“已定价持仓权重之和”，无价时可能小于 100%；文本不显示合计占比。  
    原因：无价标的不计入市值分母，写死 100% 会误导。测试：`ReportTests.test_html_and_text`（结构）与 `test_no_price`（无价路径）。

13. **SPEC 5.9 / `reset` JSON**  
    实现返回 `deleted` 是实际删除的绝对路径列表，可能 1–3 项（db/wal/shm）；无库 → 1 `no_data`。  
    原因：让用户知道删了什么；不删除 reports 和原始 CSV。测试：`CliTests.test_demo_backup_reset`。

14. **SPEC 5.1 / `init` JSON**  
    实现额外返回 `dbPath`、`created`、`schemaVersion`；文本仍只输出“已就绪”。  
    原因：脚本需要知道是否新建；不改变文本契约。测试：`CliTests.test_init_version`。

15. **SPEC 4.2 / 名称最后写入者胜**  
    实现导入/快照见到非空名称就更新 `instruments.name`；报告取 `instruments.name`。多账户对同一代码名称不一致时，后导入的覆盖。  
    原因：不新增 `transactions.name` 列，保持 schema 小；名称只是展示。测试：`CsvTests.test_trade_parse_and_bom`、`ReportTests.test_data_and_json`。

16. **SPEC 7 / 数据来源去重**  
    `sources` 按文件名去重，保留最后一次导入的 `rows_read`；`status` 仍按每次导入展示。  
    原因：同一文件多次导入时，报告不应重复列同一来源。测试：`CliTests.test_import_and_idempotent`。

17. **SPEC 5.5 / `--open` 隐含 HTML**  
    `--open` 未显式给 `--format` 时按 HTML；显式 `--format text` 仍报 2 `bad_config`。  
    原因：符合“打开报告”的直觉，又保留冲突检测。测试：`CliTests.test_report_conflicts`。

18. **SPEC 3.3.1 / 不支持北交所**  
    实现只识别沪/深 6 位代码；北交所（8/4/9 开头）按 `bad_code` 拒绝。  
    原因：v0.1.0 的行情、手续费和涨跌停规则未覆盖北交所；不猜。测试：`CsvTests.test_codes`。
