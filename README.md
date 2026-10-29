# 持仓簿 Holdbook

本地优先的 A 股持仓对账与成本报告工具。导入券商交割单，拍一张持仓快照，核对数量和成本，生成单文件 HTML 报告。

```bash
pipx install .
holdbook init
holdbook import 交割单.csv --account 华泰
holdbook snapshot 持仓.csv --account 华泰
holdbook check
holdbook report --market mock
```

完整说明见后续版本；当前为开发中的 v0.1.0。
