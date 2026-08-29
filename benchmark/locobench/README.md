# LoCoBench-Agent × LansCoder

把 [LoCoBench-Agent](https://github.com/SalesforceAIResearch/LoCoBench-Agent)
(Salesforce AI Research, arXiv 2511.13998)接入 LansCoder,作为上下文管理系统的
**可复现量化评估基准**。纯测量:不改 `lanscoder/` 核心。

- 被测对象:LansCoder `ContextWindowManager` 三层压缩(L1 路由压缩 / L2 归档占位 / L3 LLM 摘要)。
- 接入形态:`LansCoderAgent(BaseAgent)` 进程内,复用 `create_agent_session`;
  LoCoBench Tool → LansCoder Tool 映射;harness 侧 `--context-management none`,
  上下文由 LansCoder 独占管理(因果干净)。
- 外部工具固定 commit 引用,clone 到工具目录(**不进仓库**),类比 Harbor。

## 目录

| 文件 | 说明 |
|---|---|
| `lanscoder_agent.py` | `LansCoderAgent(BaseAgent)`:initialize_session / process_turn / 压缩事件采集(经 `compaction_capture` 归一化) |
| `tool_mapping.py` | LoCoBench `Tool.function` → LansCoder `Tool` 映射(async 方法在独立线程新事件循环执行) |
| `compaction_capture.py` | CompactionEvent 归一化(纯函数,不依赖 locobench):before/after tokens、L1/L2/L3 hit、硬截断标记 |
| `analyze.py` | 结果聚合(纯函数):三套 token 口径 + CompactionEvent 统计(CLI 与 driver 收尾都会产出 `analysis.json`) |
| `driver.py` | CLI:`python -m benchmark.locobench.driver ...` |

## 环境准备(一次性)

LoCoBench-Agent clone 到工具目录并装依赖(数据 ~4.6GB,不进仓库):

```sh
git clone https://github.com/SalesforceAIResearch/LoCoBench-Agent.git /tmp/LoCoBench-Agent
cd /tmp/LoCoBench-Agent
python3 -m venv .venv
export all_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890
.venv/bin/pip install -r requirements.txt tiktoken psutil gdown
.venv/bin/pip install -e /Users/lansterzhang/Documents/LansCoder/packages/lanscoder-core[llm,mcp]
.venv/bin/pip install -e .   # locobench 可编辑安装

# 数据:data.zip(Google Drive,1.27GB)→ 解压出 data/
.venv/bin/gdown https://drive.google.com/uc?id=1HwPztd0bipUUi8zs7Pxo3StZCOnJBwVR
unzip -q data.zip -d .

# 验证转换缓存(8000 个已转换场景)
.venv/bin/python -m locobench.cli convert-scenarios --limit 5
```

LansCoder provider 来自 `~/.config/lanscoder/config.toml`(deepseek)。

## 跑 1 个 easy 场景(Phase 1 冒烟)

```sh
cd /Users/lansterzhang/Documents/LansCoder
mkdir -p benchmark/runs/locobench/smoke-easy-1 && cd benchmark/runs/locobench/smoke-easy-1
export all_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890
/tmp/LoCoBench-Agent/.venv/bin/python -m benchmark.locobench.driver \
  --locobench-root /tmp/LoCoBench-Agent \
  --scenario-id php_api_rest_easy_078_architectural_understanding_easy_01 \
  --max-turns 3 --model-ref deepseek/deepseek-v4-flash \
  --context-management none --output-dir .

# Phase 2 起:指定被测压缩策略(no_compact / l1_l2 / l1_l2_l3,默认 l1_l2_l3)
/tmp/LoCoBench-Agent/.venv/bin/python -m benchmark.locobench.driver \
  --locobench-root /tmp/LoCoBench-Agent \
  --difficulty hard --scenario-count 1 \
  --model-ref deepseek/deepseek-v4-flash \
  --context-management none --compaction-strategy l1_l2 \
  --output-dir benchmark/runs/locobench/hard-l1l2-1
```

产出(均在 `benchmark/runs/locobench/<run>`,gitignored):

| 文件 | 内容 |
|---|---|
| `results.json` | harness 评估结果(overall / LCBA-Comp / LCBA-Eff / turns / tool_usage / errors) |
| `transcript.json` | per-turn 统计:provider 真实 usage、harness 启发式、lanscoder chars/4、工具调用、CompactionEvent 归一化记录 |
| `analysis.json` | 聚合分析:三套 token 口径分开 + CompactionEvent 统计(attempts / L1/L2/L3 hit rate / 硬截断率 / before-after tokens) |
| `intermediate_agent_results/conversations_*/...json` | harness 完整对话转录 |
| `sessions/` | LansCoder session store(turn/压缩原始事件) |

## 口径(禁止混用)

- harness `context_tokens` / `harness_total_context_tokens`:启发式 `len(content.split())*1.3`,非真实 token。
- `tokens_used` / `input_tokens` / `output_tokens`:`ChatResponse.usage` provider 真实 usage。
- `lanscoder_chars4_estimate` / CompactionEvent `before_tokens` / `after_tokens`:
  LansCoder `(len+3)//4` 估算(含系统提示/工具 schema/历史),由 `runner.context_budget` 与
  `compaction_completed` 事件采集。
- CompactionEvent 归一化(`compaction_capture.normalize_compaction_event`):
  - `compaction_completed` = L1/L2 规则压缩(带 `level_metrics` 算 L1/L2 hit rate)。
  - `llm_compaction_completed` = L3 摘要事件;`hard_truncate=true` 表示硬截断兜底
    (`fallback_steps[].action == "hard_truncate"`),不算 L3 摘要命中。
  - `compaction_skipped` = 压缩被跳过(under_threshold / skipped_no_effect / strategy_no_compact)。

## 已知坑(Phase 1)

1. 场景文件必须嵌套在 `initial_context.project_files`(官方 CLI 结构),否则工具工作区为空。
2. LansCoder 内部 loop 的工具调用要读 session store 事件采集,`ChatResponse.tool_calls`
   只含最后一条消息。
3. `FinishReason` 是字符串 Literal,不是 enum(别用 `.value`)。
4. `--max-turns N` 才是预算上限;harness 阶段循环会在 success 条件不满足时把回合跑满。
5. 工具名带 harness 副本后缀 `_copy_<id>`,转录前归一化。
