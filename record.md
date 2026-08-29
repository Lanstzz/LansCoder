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
