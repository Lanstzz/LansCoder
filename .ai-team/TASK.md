# Current Task

- ID: `TASK-004`
- Title: `接入 LOCA-bench:上下文系统可复现评估基准`
- Status: `planning`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

把 LOCA-bench(HKUST NLP,2026,arXiv 2602.07962)接入 LansCoder,作为上下文管理系统的**可复现量化评估基准**:复用 LOCA 的 task-configs(环境长度 8K→128K/256K 可控增长)+ mock MCP 服务器 + eval 计分,让 LOCA 的 agentic 任务跑在 **LansCoder 真实 loop**(`--benchmark` + MCP 客户端)上,产出"**上下文增长 vs 准确率 / token 成本 / 压缩行为**"的对比,并与 LOCA 内置 react 基线(及可选 Claude Code 官方基线)对照。

- **评估对象**:`lanscoder/context/` 的 ContextWindowManager 三层压缩(L1 路由压缩 / L2 归档占位 / L3 LLM 摘要)与触发/回退/熔断机制。
- **接入形态**:LansCoder 侧新增 `benchmark/loca/` driver,复用 LOCA-bench 的 task-configs + mock MCP 服务器 + eval;不 fork 外部仓库(固定 commit 引用,类比 Harbor)。
- **对照项**:no-compact / L1+L2 / L1+L2+L3 / LOCA `--context-reset` / LOCA react 基线。

## Acceptance scenarios

- [ ] **SC-1 (最小闭环)**: Given LOCA 8K 档单任务 + 便宜模型,When 由 LansCoder(`--benchmark` + MCP 指向 mock 服务器)执行,Then LOCA eval 正常出分(非基础设施失败)。
- [ ] **SC-2 (基线可复现)**: Given 同档位 + 同模型,When 运行 LOCA 内置 react 基线,Then 能复现官方基线形态(acc/steps/token 输出齐全)。
- [ ] **SC-3 (可复现命令)**: Given 已装 LOCA-bench(固定 commit),When 运行 `python -m benchmark.loca run ...`,Then 产出 `results.json` + trajectory + `token_stats.json`(LOCA 格式)且退出码 0。
- [ ] **SC-4 (压缩 A/B)**: Given 至少一组任务子集 + 档位,When 对比 no-compact 与 L1+L2(+L3),Then 产出"上下文增长-准确率 / token 成本 / 压缩行为(hit rate、before/after、硬截断率)"对比图。
- [ ] **SC-5 (门禁)**: When 运行 `pytest` + `ruff check .` + `node .ai-team/check.mjs --base origin/main`,Then 全绿(新增 benchmark/loca 不碰 `lanscoder/` 核心语义)。

## Invariants

- 不 fork / 不修改 LOCA-bench 源码;外部工具固定 commit 引用,clone 到工具目录(不进仓库),类比 Harbor(`harbor==0.18.0`)。
- `benchmark/loca/` 只做 driver + 分析脚本,**不改 `lanscoder/` 核心行为**(上下文系统语义零改动;本任务只做"测量",不做"优化")。
- 不提交 API key、mock 数据、私有会话;结果只保留聚合统计与图。
- 预算可控:任务子集(4–6 个)× 档位(8K/32K/64K 起步)× 策略 × 单一便宜模型(deepseek-chat 优先);全矩阵仅预算允许时扩展。
- token 口径差异(LOCA 用 GPT-4 tokenizer 量环境长度,`lanscoder` 用 chars/4 估算)必须在分析中标注,不可混用。
- 沿用"仓库只维护 benchmark 适配器"原则:LOCA-bench 是本仓库继 Harbor 之后唯一新增的 benchmark 集成。

## Decisions

- **D1(已定,用户)** 选定 **LOCA-bench** 作为上下文系统评估基准(对比候选:SWE-bench Verified 认知度最高但对上下文 claim 不可归因;RULER/LongBench v2 为模型级;Factory probe 方法论为自建 harness)。理由:唯一把"上下文增长"做成独立变量的 agent 级基准,因果可归因 + 自带 Claude Code 基线。
- **D2(已定,调研)** 接入形态 = **LansCoder 侧 driver 复用 LOCA 的 task-configs + mock MCP 服务器 + eval**,对标 LOCA 现有 `run-claude-agent`(外部 agent 接入路径);不新增 LOCA 内嵌策略(避免侵入外部仓库)。
- **D3(已定,调研)** LansCoder 侧接入点成立:`lanscoder/cli.py` 已有 `--benchmark` 非交互模式(bypass 权限、关预写审查、可设工具轮上限,`run_benchmark_turn`);`lanscoder/mcp/` 已有完整 MCP 客户端(stdio + streamable_http,`mcp__<server>__<tool>`)。兜底方案:LOCA `run-claude-api` 形态——LansCoder 直连 OpenAI 兼容端点,绕过 MCP 层。
- **D4(草案,待 Phase 0/1 验证)** LOCA 上下文增长靠 agent 经工具输出逐步累积;需验证 LansCoder 压缩在真实任务中确实触发(触发水位高 90% / 低 72%),否则改用更大档位(64K+)或注入环境预载。
- **D5(草案,待 Phase 2)** `benchmark/loca/` 的压缩 A/B 通过现有装配参数暴露(不新增核心 API),并收集 `CompactionEvent`(before/after tokens、L1/L2/L3 hit rate、硬截断率)与 LOCA `token_stats.json` 关联。

## Completed

- [x] 调研 `lanscoder/context/` 全模块(manager/compaction/archive/llm_compact/token_budget/tool_lifecycle/runtime_state 等),产出竞品对比结论:压缩管线健壮(L1/L2/L3 + 生命周期失效 + 序列完整性与护栏),短板为 token 计量(chars/4,中文失真)、无 prompt caching、无 repo map、每次请求全量重放 O(n²)(2026-08-29,对话记录,未落代码)。
- [x] 调研 benchmark 生态:Factory probe 方法论 + hermes-compression-eval(压缩保真)、CompInt(约束完整性)、LOCA-bench(agent 级上下文增长)、RULER/LongBench v2/LongMemEval/LOCOMO(长上下文)、SWE-bench Verified(通用货币);选定 LOCA-bench 为单跑推荐(2026-08-29)。
- [x] 确认 LansCoder 侧接入点:`--benchmark` 非交互模式(cli.py `run_benchmark_turn`)+ MCP 客户端(`lanscoder/mcp/`,stdio/streamable_http);Harbor 适配器(`benchmark/harbor/lanscoder_agent.py`)已有 `lanscoder --benchmark --project .` 调用先例(2026-08-29)。

## Pending

- **Phase 0(下一步)**:clone LOCA-bench 到 /tmp(需联网+审批),通读 `run-claude-agent` 接入模板、mock MCP 服务器启动方式、eval 计分、`token_stats.json` 口径;确认 LansCoder `--benchmark` 的 MCP 注入与压缩开关路径;产出集成点清单 + 可行性结论。
- **Phase 1**:8K 档单任务最小闭环(LOCA react 基线 + LansCoder 各跑一遍),验证压缩确实介入。
- **Phase 2**:`benchmark/loca/` driver 自动化(config 解析 / mock server 生命周期 / 会话装配 / CompactionEvent 收集 / results 对齐)。
- **Phase 3**:实验矩阵(子集 × 8K/32K/64K × 策略)+ 分析脚本(曲线图)。
- **Phase 4**:`benchmark/loca/README.md` 复现文档;全量门禁。

## Next step

Phase 0:在 /tmp clone LOCA-bench(固定 commit),通读关键代码并确认 LansCoder 侧 MCP 注入与压缩开关;产出集成点清单 + 可行性结论,更新本任务状态为 `active` 后开始 Phase 1。

## Verification

- [ ] `node .ai-team/check.mjs --base origin/main` → valid
- [ ] `node .ai-team/session.mjs validate` → valid(private sessions 已启用)
- [ ] `pytest` 全量绿(新增 benchmark/loca 不改核心,现有测试保持绿)
- [ ] `ruff check .` → All checks passed
- [ ] 任一 LOCA 任务 `eval.json` 出分且非基础设施失败(SC-1)

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-004 **planning**——选定 LOCA-bench 作为上下文系统评估基准,接入形态为 LansCoder 侧 driver 复用 LOCA 的 task-configs + mock MCP 服务器 + eval;验收 5 条(最小闭环/基线可复现/可复现命令/压缩 A/B/门禁)。下一步 Phase 0:clone LOCA-bench 通读代码并验证接入点。
