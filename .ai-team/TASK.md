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

- [x] **SC-1 (最小闭环)**: Given LoCoBench 数据就绪,When 用 **1 个 easy 场景**由 `LansCoderAgent` 执行,Then 跑通并产出 `AgentEvaluationResults`(非基础设施失败)。
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
- [x] **Phase 1 数据与环境(2026-08-29)**:LoCoBench-Agent clone 到 `/tmp/LoCoBench-Agent`(commit `2ab9218`,工具目录不入仓库);venv 装依赖(requirements 去 lighteval/boto3 + tiktoken + psutil + gdown);`data.zip` 1.27GB 经代理 gdown 下载并解压(4.6GB,1000 项目 / 8000 场景 / 8000 已转换 agent 场景);`convert-scenarios --limit 5` 验证通过(全 8000 走缓存,0 失败)。
- [x] **Phase 1 最小闭环跑通(2026-08-29)**:`benchmark/locobench/` 初版 driver 就绪(`lanscoder_agent.py` / `tool_mapping.py` / `driver.py` / `README.md`)。1 个 easy 场景(`php_api_rest_easy_078_architectural_understanding_easy_01`,19K context)由 `LansCoderAgent` + deepseek-v4-flash 执行 **3 turns**,产出 `AgentEvaluationResults`:**overall 0.704 / LCBA-Comp 0.74 / LCBA-Eff 0.65**,session_status=completed,0 error,59 条工具调用(6 类 LoCoBench 工具 + LansCoder `read_memory`),provider 真实 usage 共 131,327 tokens,**未触发压缩**(19K 场景 vs 1M 窗口,符合预期)。产物在 `benchmark/runs/locobench/smoke-easy-1/`(gitignored)。
- [x] **Phase 1 坑(已修/已记录)**:① harness 场景文件必须嵌套在 `initial_context.project_files`,否则工具工作区为空;② LansCoder 内部 loop 工具调用需从 session store 事件采集(`ChatResponse.tool_calls` 只含末条消息);③ `FinishReason` 是字符串 Literal 不是 enum(首次跑 `'str' object has no attribute 'value'` 中断,已修);④ `--max-turns N` 才是预算上限(阶段循环 success 不满足会跑满回合);⑤ 工具名带 `_copy_<id>` 副本后缀需归一化。

## Pending

- **Phase 2(下一步)**:完善 `benchmark/locobench/` driver:① 给 `create_agent_session` 暴露压缩策略开关(no-compact / L1+L2 / L1+L2+L3,现为默认全量),② 验证 `CompactionEvent` 在 hard/expert 场景真实采集(before/after tokens、L1/L2/L3 hit rate、硬截断率),③ 结果对齐(harness 启发式 context_tokens / provider usage / lanscoder chars/4 分开输出)。
- **Phase 3**:hard + expert 各 1 个冒烟,记录压缩触发情况与水位。
- **Phase 4**:策略 A/B(no-compact / L1+L2 / L1+L2+L3)+ 分析脚本(上下文规模-指标曲线、压缩行为统计)。
- **Phase 5**:`benchmark/locobench/README.md` 复现文档完善;更新 `record.md`(补 Phase 1 评估观察);全量门禁(SC-6)。

## Next step

**Phase 1 已通过**(数据就绪 + 1 个 easy 跑通出分)。下一步 **Phase 2**:完善 `benchmark/locobench/` driver——给 LansCoder 压缩策略加 A/B 开关(no-compact / L1+L2 / L1+L2+L3),并把 CompactionEvent 采集对齐到 hard/expert 场景(验证 before/after tokens、L1/L2/L3 hit rate、硬截断率),随后进入 Phase 3 hard+expert 各 1 个冒烟。

## Verification

- [ ] `node .ai-team/check.mjs --base origin/main` → valid
- [ ] `node .ai-team/session.mjs validate` → valid(private sessions 已启用)
- [x] SC-1:1 个 easy 场景跑通并出分(overall 0.704 / comp 0.74 / eff 0.65,3 turns,0 error)
- [ ] SC-2:hard + expert 各 1 个跑通
- [ ] SC-3:三组策略对比产出
- [ ] `pytest` / `ruff check .` 不受影响(不改核心)

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-004 **active**——LoCoBench-Agent 接入(纯测量,不改核心),决策 Q1–Q5 已定。**Phase 1 完成**(2026-08-29):数据 4.6GB 就绪、convert-scenarios 验证通过、`benchmark/locobench/` 初版 driver 跑通 1 个 easy 场景(SC-1 ✅,overall 0.704,3 turns,0 error)。下一步 **Phase 2**:压缩策略 A/B 开关 + CompactionEvent 采集对齐,再进 hard/expert 冒烟。
