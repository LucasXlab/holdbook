# 进度

项目：**持仓簿 Holdbook** — 个人投资者的本地 A 股持仓对账工具（Python 标准库 + SQLite，CLI + 单文件 HTML 报告）。

## 阶段 1：选题
- 状态：已完成
- 产出：`docs/选题.md`（8 个候选、42 条证据链接、竞品拆解、打分）
- 结论：候选 1 原始分最高；写规格前做数据可得性核对，改为候选 2「跨券商成本与资本利得账本」，定名持仓簿；修订说明在 `docs/选题.md` 文首，重估在第九节。
- 参照：`notes/参照.md`（25 条，注明出处）。

## 阶段 2：规格
- 状态：已完成（审阅模式下用户已表示继续）
- 产出：`SPEC.md`（约 800 行，唯一契约）、`DESIGN.md`（163 行）、`docs/external-apis.md`。
- 已做：两轮独立子代理审稿（40 条 + 20 条）；重写成本/XIRR 公式、对账 7 状态、金额/费用语义、报告 JSON、行情 URL/GBK、对比度、测试 seam；新增 `imports`、`snapshots.name`、`interest`、`--as-of`/`--strict`/`--json` 边界、63 条测试要求。
- 遗留：腾讯接口无官方文档，字段按 2026-10-29 实测固定；见 `docs/external-apis.md`。

## 阶段 3：开发
- 状态：已完成
- 代码：`holdbook/`（config/db/csvio/ledger/reconcile/metrics/market/report/cli/demo），运行时零第三方依赖。
- 测试：`tests/`，96 个离线用例；`python3 -m unittest discover -s tests -v` 连续 3 次全绿，无 skip。
- 本地提交：`6884f53` 骨架 → `4649d60` 行情/报告修正 → `8dc8af5` 测试 → `7a9d927` 文档；后续按功能继续本地 commit。
- 未做远程仓库、push、发布；按用户要求只保留本地历史。

## 阶段 4：验收和审查
- 状态：进行中
- 已完成：全新克隆 `/tmp/hbfresh`，`pip install .` 2.14 秒；按 README 跑通 init/import/snapshot/check/report/explain/status/backup/demo；10 次真实流程与 5 个异常记录在 `notes/安装实录.md`。
- 已修：README `--out` 缺 `--format html`；status 把 CASH 算标的；无价警告 50 条聚合；demo 无数据来源；CSP 缺失；mock 表外价格；报告/备份 0600；schema 冲突识别；账户名 >40 校验。
- 截图：`docs/screenshots/report-light.png`、`report-dark.png`、`report-375.png`（真实 Chrome/CDP，375 全页 2038px；DOM 检查无横向滚动）。
- 待完成：三路独立审稿、修复 findings、最终全新安装与三连跑。

## 阶段 5：文档和发布准备
- 状态：进行中
- 已完成：中文 README（含真实截图、适用/不适用、性能实测）、`README_EN.md`、`docs/features.md`、`development.md`、`deploy.md`、`deviations.md`、`external-apis.md`、`CHANGELOG.md`、`LICENSE`、`.env.example`、`RELEASE.md`、GitHub Actions（CI 矩阵 + tag 发布）。
- 待完成：审稿后更新数字/截图、最终 `v0.1.0` 本地 tag、最终总结。

## 已知问题
- 腾讯行情接口没有官方文档，字段靠实测；`live` 失败时按无价处理，不编造。
- 不覆盖北交所、基金穿透、期权/期货/融资融券、多币种。
- 单文件上限 100 MiB / 20 万行；超过拒绝，不做流式处理。
- 本机只在 Python 3.13.5 验证；CI 覆盖 3.11/3.12/3.13，但本次没有在三个版本逐一实跑。
