# Repository instructions

- 开工前先读 [docs/DEV_GUIDE.md](./docs/DEV_GUIDE.md)。§8 列的不变量不能破坏。
- 仓库不是运行目录。线上代码在 `~/.hermes/`，改完跑 `python3 deploy.py` 才会生效。排查线上问题以 `~/.hermes/` 里的代码和日志为准。
- 提交前跑一遍 `python3 -m unittest discover -s tests`，要在仓库根目录跑。如果动了类型或 except，再跑 `uvx ruff@0.17.3 check` 和 `uvx pyright@1.1.414`。

## Commit message

```
<type>: <中文一句话，说清改了什么、效果是什么>

<正文：中文，按需分条>

Co-Authored-By: ...
```

- `type` 只用这几个：`feat` / `fix` / `refactor` / `test` / `docs` / `ci` / `chore`。冒号后面空一格，标题末尾不加句号。
- 标题写用户能看到的效果，比如「三丽鸥主题下主页入口识别，不再被切回日间主题」。不要只写改了哪个函数。
- 正文写这几件事：
  - 为什么要改，包括现象和根因
  - 改了哪些地方
  - 怎么验证的：单测数、跑了哪些 Python 版本、有没有真机验证。真机没验就直说
  - 同步改了哪些文档，比如 DEV_GUIDE 的哪一节
- 一个 commit 只做一件事。顺手做的无关改动单独提交。
- 不要在 commit 或 PR 里写会话链接（`Claude-Session:` 或 `https://claude.ai/code/session_...`）。
