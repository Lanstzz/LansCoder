# Topic Self-Play Benchmark 使用说明

这个 benchmark 用来观察一个 coding agent 是否能稳定判断“用户的新消息还属于当前任务，还是已经切换成新任务”。

它不是完整评测 agent 编码能力的 benchmark，而是专门评测任务边界和 `topic_hash` 维护能力。这个能力后续会影响上下文压缩、checkpoint、resume、历史归档和多轮任务追踪：如果 agent 把不同任务错误合并，旧上下文会污染新任务；如果 agent 把同一任务错误切断，又会丢失必要上下文。

它也不是当前 FirstCoder `AgentLoop` 的端到端 benchmark：runner 自己实现 `continue/new_task` 和 `topic_hash` 协议，不会调用 `TaskBoundaryClassifier`、session JSONL 回放或真实 provider 请求投影。因此它适合观察任务边界概念和模型行为，不能直接当作 FirstCoder 隐藏分类机制的回归分数。

## 角色

这个 benchmark 一轮里最多有三个模型角色：

- 用户模拟器 `user simulator`：扮演用户，生成下一轮用户消息。它可以读取 sandbox 里的文件，让请求更像真实 coding 场景。
- 任务追踪 agent `tracker agent`：扮演被测 agent。它要回复用户，同时判断这条用户消息是 `continue` 还是 `new_task`，并维护 `topic_hash`。
- 裁判 `judge`：可选角色。它会复核用户模拟器给出的标准答案是否合理，避免用户模拟器自己给错 `gold_decision`。

默认只运行用户模拟器和 tracker agent；只有传入 `--judge-model` 时才启用 judge。

## 每轮流程

每一轮按这个顺序执行：

1. 用户模拟器读取当前任务摘要、上一轮用户消息和当前 `topic_hash`。
2. 用户模拟器生成新的用户消息，并给出隐藏标准答案 `gold_decision`。
3. 如果启用了 judge，judge 会审核这个 `gold_decision`。
4. tracker agent 看到新的用户消息，输出 `decision`、`topic_hash`、`reason` 和正常回复。
5. runner 比较 tracker agent 的判断和标准答案，记录分数与 transcript。

因此，用户模拟器可以读到初始 sandbox 文件，也可以在后续轮次读到 tracker agent 之前写入的文件；但它不能读到同一轮 tracker agent 刚写的文件，因为同一轮里用户模拟器先运行。

## Sandbox 的用途

`sandbox/` 是这个 benchmark 的小型工作区。它事先放了一些课程作业、论文大纲、todo 和脚本文件，用来让用户模拟器生成有文件依据的请求。

准备这些文件的原因是：真实 coding agent 的任务边界通常不是纯聊天问题，而是围绕文件逐步变化。例如用户可能先让 agent 修改 Python 作业，下一轮继续修测试，再下一轮突然切到写英文论文。没有可读文件时，用户模拟器只能凭空编请求，评测压力会弱很多。

工具权限如下：

- 用户模拟器只能使用 `list_files` 和 `read_file`。
- tracker agent 可以使用 `write_file`、`read_file`、`list_files` 和 `run_python`。
- 所有工具路径都限制在 `--sandbox-root` 内，不能逃出 sandbox。

## 输出有什么用

默认会生成两个文件：

- `runs/topic_selfplay.jsonl`：每轮一行 JSON，适合后续统计准确率、错误类型和模型对比。
- `runs/topic_selfplay.md`：可读 transcript，适合人工复盘用户消息、agent 判断、工具调用和判分理由。

重点看这些字段：

- `gold_decision` / `judge_decision`：标准答案认为是继续旧任务还是新任务。
- `tracker_decision`：tracker agent 的判断。
- `previous_hash` / `tracker_hash`：hash 是否按规则延续或更新。
- `decision_correct`：任务边界判断是否正确。
- `hash_behavior_correct`：`topic_hash` 行为是否正确。

## 目录结构

- `runner.py`：benchmark 主程序。
- `sandbox/`：测试沙箱。
- `runs/`：默认输出目录。

## 基本运行

在项目根目录执行：

```powershell
python benchmark/topic_selfplay/runner.py --rounds 5
```

默认读取 `OPENAI_API_KEY`，并使用：

- 用户模拟器模型：`gpt-4.1-mini`
- tracker agent 模型：`gpt-5-mini`
- judge：默认不启用

## 常用参数

```powershell
python benchmark/topic_selfplay/runner.py `
  --rounds 10 `
  --tracker-model gpt-5-mini `
  --user-model gpt-4.1-mini `
  --judge-model gpt-5-mini `
  --encourage-user-file-reading
```

- `--rounds`：运行轮数。
- `--sandbox-root`：测试沙箱路径，默认是 `benchmark/topic_selfplay/sandbox`。
- `--reset-sandbox / --no-reset-sandbox`：是否在运行前重置沙箱，默认重置。
- `--out`：JSONL 输出路径。
- `--transcript-out`：可读 Markdown transcript 输出路径。
- `--max-tool-rounds`：tracker agent 每轮最多工具调用轮数。
- `--max-user-tool-rounds`：用户模拟器每轮最多只读工具调用轮数。
- `--encourage-user-file-reading`：鼓励用户模拟器先读取文件再生成用户消息。

## 环境变量

没有显式传参时，runner 会从环境变量读取配置：

- `OPENAI_API_KEY`：默认 API key。
- `OPENAI_API_BASE`：默认 API base，默认 `https://api.openai.com/v1`。
- `USER_API_KEY` / `USER_API_BASE` / `USER_MODEL`：用户模拟器配置。
- `TRACKER_API_KEY` / `TRACKER_API_BASE` / `TRACKER_MODEL`：tracker agent 配置。
- `JUDGE_API_KEY` / `JUDGE_API_BASE` / `JUDGE_MODEL`：judge 配置。
