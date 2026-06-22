# Local Pytest Benchmark

这是一个给 FirstCoder 用的轻量级 coding-agent benchmark。它不像 SWE-bench 那样拉 Docker 镜像，而是把每道题生成为一个本地小 Python 仓库，让 FirstCoder 修改代码，然后用本地 `pytest` 判分。

它适合现在这个阶段：

- 快速看 agent loop 是否会读题、找文件、改代码、跑测试、停止。
- 不依赖 Docker，也不依赖远端镜像。
- 可以自己追加题目，用很小成本持续调教提示词和工具循环。

## 运行

先确保 provider 环境变量已经配置好，例如：

```sh
export FIRSTCODER_PROVIDER=openai
export FIRSTCODER_BASE_URL=...
export FIRSTCODER_API_KEY=...
export FIRSTCODER_MODEL=...
```

然后跑样例题：

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

如果你用的是 Codex bundled Python，也可以替换成：

```sh
/Users/x/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

## 题目格式

题库是 JSONL，每行一道题：

```json
{
  "id": "string_normalize",
  "title": "Normalize Usernames",
  "files": {
    "src/text_tools.py": "def normalize_username(value: str) -> str:\n    return value.strip()\n",
    "tests/test_text_tools.py": "from src.text_tools import normalize_username\n..."
  },
  "problem_statement": "Fix src/text_tools.py ...",
  "test_command": "python -m pytest -q"
}
```

默认样例在：

```text
benchmark/local_pytest/tasks.sample.jsonl
```

## 输出

summary 会记录：

- 题目 id 和标题
- 题目仓库路径
- pytest 是否通过
- FirstCoder transcript 路径
- 生成的 git diff
- 耗时和 pytest 输出

这不是主流榜单分数，但很适合做本地调试探针。等这个小 benchmark 稳定后，再把同一套 loop 放回 SWE-bench Lite 或 `swe-bench-fast`。
