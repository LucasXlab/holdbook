# 开发与测试

## 环境

- Python 3.11+（CI 覆盖 3.11/3.12/3.13；本机 3.13.5）。
- 运行时零第三方依赖，只用标准库：`sqlite3`、`csv`、`argparse`、`decimal`、`hashlib`、`pathlib`、`datetime`、`urllib`、`html`、`http.server`（测试）。
- 不需要 Node、Docker、数据库服务或 API key。

## 目录

```text
holdbook/
  config.py      环境变量、路径、日期/时刻
  db.py          SQLite schema、连接、事务、备份
  csvio.py       编码/分隔符/表头/数字/代码/行解析
  ledger.py      重放、成本、已实现、现金流
  reconcile.py   对账 7 状态与提示
  metrics.py     XIRR
  market.py      腾讯行情、mock、缓存
  report.py      文本/HTML/JSON、原子写
  cli.py         命令、退出码、错误输出
  demo.py        离线演示
tests/
  test_core.py   配置/CSV/账本/DB/对账/XIRR
  test_cli.py    端到端命令
  test_market.py 本地假 HTTP：成功/429/5xx/404/畸形/超时
  test_report.py 报告数据/JSON/HTML/无价/行情失败
  test_spec_edges.py 规格边界与回归
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前 96 个测试，全部离线。约定：

- 每个测试用 `tempfile.TemporaryDirectory()`；不写用户数据目录。
- 固定 `HOLDBOOK_NOW=2026-10-29T12:00:00Z`；需要“今天”时传 `--today`。
- 行情测试启动本地 `http.server`，把 `HOLDBOOK_QUOTE_BASE_URL` 指到 `127.0.0.1`；`fetch` 的 `sleep` 参数注入 `lambda _: None`，不等待真实退避。
- 不用固定 `time.sleep` 等业务结果；需要等待时轮询可观察状态。
- 错误用例断言 `code` 和退出码，不只断言“报错”。
- 原子写测试用临时目录；替换函数可注入，便于模拟 `os.replace` 失败。

三连跑：

```bash
for i in 1 2 3; do python3 -m unittest discover -s tests -q || exit 1; done
```

## 设计要点

- 所有金额/数量用 `Decimal`，内部 12 位，输出量化；禁止 float 入账。
- 卖出成本先乘后除，避免 4 位均价漂移。
- 写库用 `BEGIN IMMEDIATE` + 事务；`row_hash` 唯一约束做导入幂等。
- 报告 HTML 只用 DESIGN 的 token；无 JS、无外部资源；`--out -` 时 HTML 只走 stdout。
- 错误对象统一 `{ok:false,error:{code,message}}`；人类模式 `错误: ... (code=...)` 到 stderr。
- 不打印密钥；本工具本来也没有密钥。

## 加一个功能

1. 先改 `SPEC.md`（字段、命令、退出码、错误码、数值限制、测试要求）。
2. 先写失败测试，再实现；新行为要有对应 `docs/features.md` 说明。
3. 若与规格不同，在 `docs/deviations.md` 加编号，写清“规格/实现/原因/测试”。
4. 更新 `CHANGELOG.md` 和 `PROGRESS.md`；一个功能一个本地 commit，消息写用户可见行为与关键数字。
5. 跑三连测试；再跑 `holdbook demo` 和一次真实导入。

## 发布

见根目录 `RELEASE.md` 的检查单（如果存在）或：

```bash
python3 -m unittest discover -s tests -v
python3 -m build
git tag v0.1.0
```

标签前确认 `pyproject.toml` 的 `version` 与 tag 一致；CI 会拒绝不一致的 tag。不要在有未提交改动时打标签。
