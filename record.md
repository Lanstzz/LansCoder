# 已知短板记录 (Known Issues / Backlog)

> 用途:记录调研/评估中发现的、**本任务不修复**的已知短板,供后续另开任务修复。
> 创建:2026-08-29(TASK-004 调研阶段,对话结论整理)

## 上下文系统(context)已知短板

来源:2026-08-29 对 `lanscoder/context/` 全模块通读 + 竞品对比(Claude Code / Cursor / Aider)。
状态:**未修复**;修复动作另开任务(预期 TASK-005+),LoCoBench-Agent 评估结果作为追加证据。

| # | 短板 | 位置 | 影响 | 建议修复方向 |
|---|------|------|------|--------------|
| P1 | 每次请求全量重放会话 | `lanscoder/agent/session.py` `rebuild_view()` → `context/store.py` `rebuild_session_view()`;`session/index.py` `update_event` 每次 append 重读全量 JSONL + 重写索引 | 长会话 O(n²) I/O,每工具轮都全量重建 | 内存增量视图 + 脏标记;索引改为增量/异步 |
| P2 | token 估算用 `(len+3)//4` 启发式 | `lanscoder/context/token_budget.py` `estimate_text_tokens`;`providers/types.py` `ProviderCapabilities` 无 `context_window` 字段(默认 200K `assumed`) | 代码/中文严重失真(中文低估 4–12 倍),压缩水位(90%/72%)失真,可能误触发/撞 PROMPT_TOO_LONG | 接 per-model tokenizer(tiktoken/anthropic);capabilities 上报真实窗口;CJK 加权 |
| P3 | 无代码库级上下文(repo map / 符号索引 / 检索) | 全仓无 tree-sitter/symbol index/BM25 | 大仓下 agent 盲目探索,文件反复进出上下文 | tree-sitter 符号 + token 预算注入 repo map;编辑失效 |
| P4 | L3 摘要单段平铺,压缩后不恢复工作集 | `lanscoder/context/llm_compact.py` `_checkpoint_summary_message`(单条 user 消息) | 丢失"哪些文件/命令/决定"结构;压缩后模型需自己想起 re-view 文件 | 两级结构化摘要;压缩后恢复最近活跃文件引用 |
| P5 | 无 provider prompt caching | `lanscoder/context/system_prompt.py` `PromptPrefixCache`(仅内存防重建,无 `cache_control`);动态段(provider/memory_index/permission)塞进 system prompt | 长会话重复计费前缀成本;动态段破坏缓存 | Anthropic `cache_control: ephemeral`;动态段移出稳定前缀 |
| P6 | 压缩触发面窄(无 idle/microcompact) | `lanscoder/context/triggers.py` 仅 token 水位 | 挂起久后继续,保留一坨已冷却旧内容 | idle 触发 + 旧 tool_result 清理 |
| P7 | 压缩在请求路径内同步执行,L3 摘要增加首 token 延迟 | `lanscoder/agent/loop.py` `_prepare_main_provider_request` → `compact_if_needed` | 长会话每轮请求前可能阻塞 | 空闲后台预压缩/流式摘要(待评估) |

## 记录方式

- 每个新发现:加一行到上表(位置/影响/建议方向)。
- 修复时:在对应行标注 `→ 已修复(TASK-xxx)`,不删除,保留历史。

---

## TASK-004 评估观察(2026-08-29,Phase 1)

> LoCoBench-Agent 1 个 easy 场景冒烟(`php_api_rest_easy_078_architectural_understanding_easy_01`,
> 19K context,deepseek-v4-flash,3 turns)后的测量观察;均为**测量侧**发现,修复另开任务。

| # | 观察 | 证据 | 影响 / 建议 |
|---|------|------|-------------|
| O1 | harness `context_tokens` 启发式 `len(split())*1.3` 与 provider 真实 usage 偏差大 | 单 turn 启发式 vs provider input 34K–46K(含 LansCoder 系统提示/工具 schema/历史) | 与 P2 同源:分析时必须按口径分开,禁止混用;L1/L2/L3 压缩效果曲线应使用 provider 真实 usage 或 tiktoken 统一计量 |
| O2 | LansCoder 内部工具 `read_memory` 混入工具调用统计 | turn 1 工具列表含 `read_memory` | harness `tool_usage_log` 会把它算进 agent 工具使用;分析时需过滤非 LoCoBench 工具或标注 |
| O3 | easy(19K context)场景不触发压缩 | 3 turns 0 个 CompactionEvent(1M 窗口) | 符合预期;压缩行为验证必须用 hard/expert(200K–1M)场景(Phase 3 冒烟验证) |
| O4 | 回合预算由 `--max-turns` 决定,harness 阶段循环在 success 不满足时会跑满 | 首次 10 turns ≈ 30+ 次串行 LLM 调用,~10 分钟 | 冒烟/预算控制固定 `--max-turns`;批量跑时按场景难度设上限 |
| O5 | 同一回合三套 token 口径差距大(easy 场景量化) | Phase 2 复跑(easy,19K 场景):末回合 harness 启发式 ~7.4K vs lanscoder chars/4 ~40K vs provider 真实 usage 45K/回合(累计 127K) | 与 P2/O1 同源:分析/画曲线必须按口径分开(transcript/analysis 已分开标注);压缩效果曲线建议用 provider 真实 usage 或 tiktoken 统一计量 |
| O6 | 原生窗口下 hard/expert 不触发压缩;降到 200K 窗口后稳定触发 | Phase 3 冒烟:hard/expert 场景自身 `context_window_tokens`=1M/1.5M 时,初始上下文 chars/4 最大仅 ~319K vs 高水位 ~851K+(ratio≈0.38);W=200K(高水位 ~163K)时 12 次压缩全部触发(before 167–181K) | 量化 P2:chars/4 估算需比真实 token 口径小 ~5–7.5x 的窗口才能命中水位;A/B 对比必须固定窗口并标注口径 |
| O7 | 代码理解类任务上 L1+L2 单独即可压到达标,L3 与硬截断 0 次 | Phase 3 hard/expert 各 6 次压缩全部 `stopped_at=l2`(L3_events=0, hard_truncate=0);L2 归档占位贡献 85–95% token 节省(l2 saved 40K–139K/次),L1 路由压缩仅 0–28K/次 | 对 code-comprehension 类,L3 摘要不是瓶颈;L3 价值需在更长会话/非代码任务上验证(Phase 4 A/B 可对比) |
| O8 | provider 真实 usage 远大于 lanscoder chars/4 与 harness 启发式(三口径再量化) | Phase 3:provider input 862K(hard)/713K(expert) tokens vs lanscoder chars/4 末值 167K/126K vs harness 启发式 ~2.9K | 与 P2/O1/O5 同源:harness 启发式只统计 assistant 文本,不反映工具结果上下文;分析必须用 provider usage 或 tiktoken |
| O9 | harness C 层关键词打分会高估"看起来在干活"的 agent,B 层 LLM judge 才能判出结果对错 | Phase 3.5 临时 judge(deepseek-v4-flash 占位):expert 场景 agent 根因找错(ground truth `module_62 EventStandardizerV3`,agent 误判 `module_21` schema),judge correctness=0.0,而 harness LCBA-Comp=0.744(命中 "understanding/analysis" 关键词);hard judge correctness=0.4 vs harness 0.731 | Phase 4 A/B 应以 B 层为主分(权重 0.65);C 层只作流程参考;正式 judge 需用与 agent 不同模型(qwen3.7-plus),当前 deepseek-v4-flash 为占位 |
