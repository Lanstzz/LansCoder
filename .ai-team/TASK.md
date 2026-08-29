# Current Task

- ID: `TASK-004`
- Title: `接入 LoCoBench-Agent:上下文系统可复现评估基准`
- Status: `active`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

把 LoCoBench-Agent(Salesforce AI Research,arXiv 2511.13998)接入 LansCoder,作为上下文管理系统的**可复现量化评估基准**(纯测量,不改 `lanscoder/` 核心语义)。

- **数据**:复用 LoCoBench-Agent 的 8,000 交互场景(10K–1M tokens、10 语言、8 任务类别、四档难度),按 `--scenario-count/--difficulty/--category` 抽子集。
- **接入形态**:LansCoder 以自定义 `LansCoderAgent(BaseAgent)` **进程内**接入(`create_agent_session` + LoCoBench 工具映射),被测对象 = `ContextWindowManager` 三层压缩(L1 路由压缩 / L2 归档占位 / L3 LLM 摘要)。
- **测量方式**:harness 跑 `--context-management none`(不干预 agent 历史),由 LansCoder 的上下文系统独占管理;对比 `no-compact / L1+L2 / L1+L2+L3` 三组,产出"上下文规模 vs 准确率/效率指标 + `CompactionEvent` 压缩行为(before/after tokens、L1/L2/L3 hit rate、硬截断率)"。
- **短板记录**:评估暴露的已知短板(如 P2 token 计量 `chars/4` 对代码/中文失真)不在此任务修复,记入 `record.md`,修复另开任务。

## Acceptance scenarios

- [ ] **SC-1 (最小闭环)**: Given LoCoBench 数据就绪,When 用 **1 个 easy 场景**由 `LansCoderAgent` 执行,Then 跑通并产出 `AgentEvaluationResults`(非基础设施失败)。
- [ ] **SC-2 (hard/expert 冒烟)**: Given SC-1 通过,When 各跑 **1 个 hard + 1 个 expert** 场景,Then 均完成并出分,记录压缩是否触发及触发水位。
- [ ] **SC-3 (策略 A/B)**: Given 同一场景子集,When 对比 `no-compact / L1+L2 / L1+L2+L3`,Then 产出对比(准确率/效率指标 + CompactionEvent 压缩行为统计)。
- [ ] **SC-4 (可复现命令)**: Given 环境与数据就绪,When 运行 `benchmark/locobench/` 的 driver 命令,Then 可复现产出 results + conversation transcript + per-turn token/context 统计。
- [ ] **SC-5 (短板记录)**: Given 评估完成,When 审阅暴露的上下文系统问题,Then 记入 `record.md`(标注不修复、待另开任务)。
- [ ] **SC-6 (门禁)**: When 运行 `pytest` + `ruff check .` + `node .ai-team/check.mjs --base origin/main`,Then 全绿(新增 benchmark/locobench 不碰 `lanscoder/` 核心)。

## Invariants

- 不 fork / 不修改 LoCoBench-Agent 源码;外部工具固定 commit 引用,clone 到工具目录(不进仓库),类比 Harbor。
- `benchmark/locobench/` 只做 driver + 分析脚本,**不改 `lanscoder/` 核心行为**(本任务纯测量)。
- 不提交 API key、大体积数据(data.zip / 场景缓存);结果只保留聚合统计与图。
- 预算可控(用户已定):第一波 **1 个 easy** 验证链路,通过后 **1 个 hard + 1 个 expert** 冒烟;模型 deepseek-chat 优先;暂不加官方基线。
- token 口径必须分开标注:harness `context_tokens`(启发式 `len.split()*1.3`)/ provider 真实 usage / lanscoder `chars/4` 估算,不可混用。
- 沿用"仓库只维护 benchmark 适配器"原则:LoCoBench-Agent 是本仓库继 Harbor 之后唯一新增的 benchmark 集成。

## Decisions

- **D1(已定,用户)** 只写 **LoCoBench-Agent**;LOCA-bench 弃用(候选对比结论保留在对话/调研记录,不写入任务)。
- **D2(已定,用户)** 分波验证:第一波 1 个 easy;跑通后各 1 个 hard + 1 个 expert。
- **D3(已定,用户)** 策略 A/B = `no-compact / L1+L2 / L1+L2+L3` 三组。
- **D4(已定,用户)** 暂不加官方 OpenAI/Anthropic 基线对照。
- **D5(已定,用户)** 纯测量;暴露的短板(如 P2 token 计量失真)记入 `record.md`,修复另开任务。
- **D6(已定,调研)** 接入形态 = `LansCoderAgent(BaseAgent)` 进程内,复用 `create_agent_session`;LoCoBench 的 `AgentFactory._create_custom_agent` 是 stub、CLI `--agent-type` 不含 custom → 自写 driver 直接实例化 harness 类。
- **D7(已定,调研)** 评估时 harness 跑 `--context-management none`,由 LansCoder `ContextWindowManager` 独占上下文管理(A/B 因果干净)。

## Completed

- [x] LoCoBench-Agent 源码调研(2026-08-29):`BaseAgent` 接口与自定义路径(`base_agent.py` / `custom_agent.py` / `agent_factory.py` CUSTOM=stub);8,000 场景结构(data.zip → `convert-scenarios` → 缓存 → `evaluate --mode agent`,`--scenario-count/--difficulty/--category` 子集);per-turn token/context 统计(`conversation_history[].context_tokens` 启发式 + `AgentResponse.tokens_used` 真实 usage + harness `ContextState.total_tokens`/`compression_history` tiktoken);harness context management(`none/basic/adaptive`,默认 adaptive,触发 0.4/0.6);`RobustAgentEvaluator` + `AgentSession` 主路径;`run_llm_evaluation` 为 random 占位符(只用 agent 模式)。
- [x] 决策确认(2026-08-29,用户 Q1–Q5):LoCoBench 主选 / 分波验证 / 三组 A/B / 不加官方基线 / 短板记 record.md 不修。

## Pending

- **Phase 1(下一步)**:下载 data.zip(Google Drive + 代理,gdown)→ `convert-scenarios` → 用 **1 个 easy** 场景跑通 LansCoder 链路(deepseek-chat),记录结果与坑。
- **Phase 2**:写 `benchmark/locobench/` driver:`LansCoderAgent(BaseAgent)` + LoCoBench Tool → LansCoder Tool 映射 + `CompactionEvent` 采集 + 结果对齐。
- **Phase 3**:hard + expert 各 1 个冒烟,记录压缩触发情况。
- **Phase 4**:策略 A/B(no-compact / L1+L2 / L1+L2+L3)+ 分析脚本(上下文规模-指标曲线、压缩行为统计)。
- **Phase 5**:`benchmark/locobench/README.md` 复现文档;更新 `record.md`;全量门禁。

## Next step

Phase 1:下载 LoCoBench 数据并跑通 **1 个 easy** 场景。先确认 data.zip 可下载(代理)与 `convert-scenarios` 可用,再以最简 `LansCoderAgent` 驱动 1 个场景,把结果(出分/轮次/token)记录到本任务,通过后进入 hard/expert 冒烟。

## Verification

- [ ] `node .ai-team/check.mjs --base origin/main` → valid
- [ ] `node .ai-team/session.mjs validate` → valid(private sessions 已启用)
- [ ] SC-1:1 个 easy 场景跑通并出分
- [ ] SC-2:hard + expert 各 1 个跑通
- [ ] SC-3:三组策略对比产出
- [ ] `pytest` / `ruff check .` 不受影响(不改核心)

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-004 **active**——LoCoBench-Agent 接入(纯测量,不改核心),决策 Q1–Q5 已定(LoCoBench 主选 / 1 easy 先行→hard+expert 冒烟 / 三组 A/B / 不加官方基线 / 短板记 record.md)。验收 6 条(SC-1..SC-6)。下一步 Phase 1:下载数据 + 跑通 1 个 easy 场景。
