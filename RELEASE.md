# 发布检查单

1. `python3 -m unittest discover -s tests -v` 三连跑全绿，无 skip。
2. `holdbook demo --dir /tmp/hb-release-demo` 成功，报告可打开。
3. 全新临时目录 `pip install .` 后按 README 快速开始跑通。
4. `pyproject.toml` 的 version、`CHANGELOG.md`、tag 三者一致。
5. README 的功能、截图、性能数字都能复现。
6. `git status` 干净；只做本地 commit/tag，不 push、不建远程仓库（除非用户明确要求）。
7. `git tag v0.1.0`；如需发布，由用户决定远程和 Release。
