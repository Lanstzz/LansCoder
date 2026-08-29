# Lessons Learned(踩坑教训)

> 本文件记录本项目协作中的教训,防止重犯。每次踩坑:一句话事件 + 根因 + 规则化后的守则。

## 2026-08-29 — 未先调研就接入 LoCoBench-Agent(测试框架选型踩坑)

### 事件

TASK-004 直接采纳了 LoCoBench-Agent(Salesforce AI Research,arXiv 2511.13998)作为上下文系统评估基准,
从 clone、装依赖、下 4.6GB 数据到跑完 easy/hard/expert 多轮 LLM 评估之后,才在归因分析中发现:

- **数据质量差**:场景 `ground_truth` 与提供的项目文件严重不一致(expert 场景 13/17 关键标识符在代码中不存在;
  `gamma` 全仓 0 次出现;`EventStandardizerV3`/`PIPELINE_ROUTING` 只存在于场景 JSON 文本)。
- **根因**:生成管线两阶段解耦——Phase 2 用 LLM 独立生成代码,Phase 3 用另一个 LLM 只凭"文件名+行数/字符数"清单
  编造 ground_truth(让它"realistic and challenging"),没有任何一致性校验 → ground_truth 是想象出来的。
- **社区几乎无人用**:GitHub 仅 **22 stars / 6 forks / 1 open issue**,2025-11 创建后 2026-06 基本停更。

代价:多轮真实 LLM 评估 + judge 打分 + 大量归因分析,结论却是一部分"测的是数据 bug 而不是 agent"。

### 根因(我的责任)

用户提出用这个框架时,我没有先做"权威性/star/社区"调研就着手实现;用户还没明确批准全量跑
(实际是"分波验证"),我就一路跑完了多轮 LLM;也没有先跑最小冒烟就放大到 hard/expert。

### 规则(以后必须遵守)

1. **先用测试集/测试框架前,先调研**:权威性(是否同行评审/正规机构)、GitHub star、活跃度、使用者数量、社区评价,
   并**把调研结论先给用户看**。
2. **发现框架有大错误 / 社区几乎没人用 / star 很低时,主动提醒用户,不要一味附和**;
   **在用户明确批准之前,不开始实现/跑量**。
3. **准备测试调用时,先跑冒烟测试**(最小链路、最小数据),冒烟过了再放大。
4. **跑 LLM 一定从最小 task、只跑一轮开始**,确认链路/成本/输出格式后再逐步加回合与难度。

### 检查清单(选型时逐项确认)

- [ ] 官方/正规机构出品?有论文且可复现?
- [ ] GitHub stars / forks / 活跃度 / 最近 push?
- [ ] 使用量 / 社区讨论 / 已知 issue?
- [ ] 数据内部一致性(ground_truth ↔ 代码/答案)抽查过?
- [ ] 已把调研结论给用户,并获得明确批准?
- [ ] 最小冒烟已跑通,才放大?

## 2026-08-29 — 沉没成本与 GIGO(续)

- **Trash in, trash out(GIGO)**:输入数据不可信,再多的流程/评分/分析也只是在给垃圾做精美包装。基准数据质量是第一关。
- **沉没成本要果断抛弃**:LoCoBench 已投入(数据 4.6GB、多轮 LLM、打分链路),但确认数据不可信后,决定整体弃用、换基准,
  不因"已经花了这么多"而继续往错误方向投入。判断标准永远是"还能不能产出可信结论",而不是"已投入多少"。
- 已做的工具链(压缩策略开关、CompactionEvent 采集、A/B 打分框架)是通用资产,换基准后可复用;但**不能**给不可信基准背书。

## 2026-08-29 — Harbor + SWE-bench 最小冒烟 SOP(沉淀,避免下个 agent 重踩)

### 核心教训

1. **跑 LLM 之前先想清楚"数据怎么拿回来"**:上次 swe-min 冒烟只拿到最终文本,拿不到工具调用/token/CompactionEvent,
   等于一半白跑。**凡是评估压缩/上下文,必须确认 session(含 CompactionEvent)能落盘并被抓取**,再开跑。
2. **环境问题按 SOP 逐步排查,不要反复试错**:平台/镜像/汇总表/日志抓取四类坑都有定论,照做即可。

### SOP(已验证的最小链路)

```sh
# 0) 前置检查
docker version                          # Docker daemon 在跑
docker run --rm --platform linux/amd64 alpine uname -m   # 应输出 x86_64(Rosetta 可用)

# 1) 装 Harbor(仓库 venv)
venv/bin/python -m pip install 'harbor==0.18.0'

# 2) 数据:查缓存,没有再下载
ls ~/.cache/harbor/tasks/packages/swe-bench/        # 500 个已缓存 task
# venv/bin/harbor dataset download swe-bench/swe-bench-verified --cache

# 3) 平台 + 基础镜像(两个坑一起治)
export DOCKER_DEFAULT_PLATFORM=linux/amd64          # swebench 镜像 amd64-only
docker pull swebench/sweb.eval.x86_64.<task>:latest # 先本地化,规避 daocloud mirror auth EOF

# 4) 单 task 最小冒烟(必带 --agent-include-logs 抓 session)
export PYTHONPATH="$PWD"
export LANSCODER_API_KEY=...                        # 从 ~/.config/lanscoder/config.toml 读取
venv/bin/harbor run -p ~/.cache/harbor/tasks/packages/swe-bench/<task> \
  -a benchmark.harbor.lanscoder_agent:LansCoderHarborAgent -m deepseek/deepseek-v4-flash \
  -n 1 -k 1 --ak max_tool_rounds=120 \
  --agent-include-logs '*.jsonl' \
  --agent-setup-timeout-multiplier 3 \
  --ae LANSCODER_PROVIDER_NAME=deepseek --ae LANSCODER_MODEL=deepseek-v4-flash \
  --ae LANSCODER_BASE_URL=https://api.deepseek.com --ae "LANSCODER_API_KEY=\${LANSCODER_API_KEY}" \
  --ae LANSCODER_DISABLE_GLOBAL_SKILLS=1 \
  --mounts '[{"type":"bind","source":"'$HOME'/.cache/lanscoder-harbor","target":"/opt/lanscoder-cache"}]' \
  -o benchmark/runs/harbor/<run> -y

# 5) 读结果(汇总表有 bug,别等它)
#    reward: jobs/<ts>/<trial>/result.json -> verifier_result.rewards.reward(1.0 = 通过)
#    agent 最终文本: agent/lanscoder.txt
#    session(工具/压缩数据): agent/lanscoder-session.jsonl(需 --agent-include-logs '*.jsonl')
```

### 四类坑(定论)

| 坑 | 现象 | 解法 |
|---|---|---|
| 平台 | `no match for platform in manifest` | Apple Silicon 需 `DOCKER_DEFAULT_PLATFORM=linux/amd64`(Rosetta) |
| 镜像源 | `failed to fetch anonymous token ... EOF` | daemon 配了 daocloud mirror 偶发故障;先 `docker pull` 本地化再跑 |
| 汇总表 | `ValueError: too many values to unpack`(task 名含 `__`) | Harbor 0.18.0 与最新版同款 bug(`_format_group_title` 按 `__` split,期望 2/3 段;eval key `agent__model__psf__requests-1142` 有 4 段);**升级不解决**,直接读 result.json;可给上游提 issue |
| 日志抓取 | 只拿到 lanscoder.txt,没有 session | 适配器已导出 `lanscoder-session.jsonl`;运行加 `--agent-include-logs '*.jsonl'` |

### 压缩 A/B 的参数注入(本次新增)

- 适配器 `LansCoderHarborAgent` 新增 `--ak context_window=200000 --ak compaction_strategy=no_compact|l1_l2|l1_l2_l3`(透传到 `lanscoder --benchmark --context-window/--compaction-strategy`)。
- **badcase 回看重放包** = 每个 trial 的 `result.json`(verifier reward)+ `agent/lanscoder.txt`(最终文本)+ `agent/lanscoder-session.jsonl`(工具调用/CompactionEvent);fail 的 trial 保留整套,作为优化 badcase。
- pass/fail 严格二元(不细分);每策略重复取均值暂不做(经济紧张),保留 CLI 配置位。
