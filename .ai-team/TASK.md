# Current Task

- ID: `TASK-004`
- Title: `接入 LoCoBench-Agent:上下文系统可复现评估基准`
- Status: `active`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

为 LansCoder 上下文管理系统(L1/L2/L3 压缩)建立**可复现量化评估基准**(纯测量,不改 `lanscoder/` 核心语义)。**2026-08-29 决策(D9):弃用 LoCoBench-Agent**——其数据 ground_truth 与代码严重不一致(见 record.md O10、LESSONS.md),star 仅 22、社区冷清,投入产出不可信。改为**先经权威性/社区/数据质量调研、用户批准后再接入的新基准**(候选见 Pending)。

- **数据**:复用 LoCoBench-Agent 的 8,000 交互场景(10K–1M tokens、10 语言、8 任务类别、四档难度),按 `--scenario-count/--difficulty/--category` 抽子集。
- **接入形态**:LansCoder 以自定义 `LansCoderAgent(BaseAgent)` **进程内**接入(`create_agent_session` + LoCoBench 工具映射),被测对象 = `ContextWindowManager` 三层压缩(L1 路由压缩 / L2 归档占位 / L3 LLM 摘要)。
- **测量方式**:harness 跑 `--context-management none`(不干预 agent 历史),由 LansCoder 的上下文系统独占管理;对比 `no-compact / L1+L2 / L1+L2+L3` 三组,产出"上下文规模 vs 准确率/效率指标 + `CompactionEvent` 压缩行为(before/after tokens、L1/L2/L3 hit rate、硬截断率)"。
- **短板记录**:评估暴露的已知短板(如 P2 token 计量 `chars/4` 对代码/中文失真)不在此任务修复,记入 `record.md`,修复另开任务。

## Acceptance scenarios

- [x] **SC-1 (最小闭环)**: Given LoCoBench 数据就绪,When 用 **1 个 easy 场景**由 `LansCoderAgent` 执行,Then 跑通并产出 `AgentEvaluationResults`(非基础设施失败)。
- [x] **SC-2 (hard/expert 冒烟)**: Given SC-1 通过,When 各跑 **1 个 hard + 1 个 expert** 场景,Then 均完成并出分,记录压缩是否触发及触发水位。
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
- **D9(已定,用户 2026-08-29)** **弃用 LoCoBench-Agent**(沉没成本抛弃;GIGO)。评估基准须**先调研**(权威性/star/社区/数据一致性)并**经用户明确批准**再接入;跑 LLM 从最小 task 单轮开始;测试前先冒烟。教训入 `LESSONS.md`。
- **D8(已定,用户 2026-08-29,Phase 3.5)** "不跑偏/结果正确"打分 = A+B+C 加权;C 只取 **LCBA-Comp**(Eff 单独报告不并入);B 层 judge 换供应商 **DashScope 模型 `qwen3.7-plus`**(与 agent deepseek-v4-flash 不同模型,避免自评);A 层用 **ground_truth 反引号标识符** 确定性提取;立即对现有 3 个 run(easy-2/hard-1/expert-1)出分。**权重:A 0.05 / B 0.65 / C 0.30**(初版 0.3/0.5/0.2 → 0.1/0.6/0.3 → 终版;A+C 实测 A 命中率 0.714/0.133/0.0,参考性低,下调 A 仅作客观 sanity、B 语义主分;可 CLI 覆盖)。

## Completed

- [x] LoCoBench-Agent 源码调研(2026-08-29):`BaseAgent` 接口与自定义路径(`base_agent.py` / `custom_agent.py` / `agent_factory.py` CUSTOM=stub);8,000 场景结构(data.zip → `convert-scenarios` → 缓存 → `evaluate --mode agent`,`--scenario-count/--difficulty/--category` 子集);per-turn token/context 统计(`conversation_history[].context_tokens` 启发式 + `AgentResponse.tokens_used` 真实 usage + harness `ContextState.total_tokens`/`compression_history` tiktoken);harness context management(`none/basic/adaptive`,默认 adaptive,触发 0.4/0.6);`RobustAgentEvaluator` + `AgentSession` 主路径;`run_llm_evaluation` 为 random 占位符(只用 agent 模式)。
- [x] 决策确认(2026-08-29,用户 Q1–Q5):LoCoBench 主选 / 分波验证 / 三组 A/B / 不加官方基线 / 短板记 record.md 不修。
- [x] **SWE-bench 最小 task 冒烟通过(2026-08-29,新基准 Phase 1)**:Harbor 0.18.0 + `benchmark/harbor` 适配器 + deepseek-v4-flash,单 task `psf__requests-1142`(requests `Content-Length` bug)单试次 → **verifier reward=1.0,0 error**(agent 定位 `prepare_content_length` 无条件写 `Content-Length: 0`,改 `requests/models.py` + 补测试)。坑:① Apple Silicon 需 `DOCKER_DEFAULT_PLATFORM=linux/amd64`(swebench 镜像 amd64-only,Rosetta 可跑);② 需先 `docker pull` 基础镜像(本机 daemon 配了 daocloud mirror,偶发 auth EOF);③ Harbor 0.18.0 汇总表在 task 名含 `__`(psf__requests-1142)时崩溃→ 直接读 `result.json`;④ 会话/token/压缩数据默认不落盘,需 `--artifact /tmp/lanscoder-harbor-sessions` 抓取(A/B 前补)。
- [x] **LoCoBench 阶段整体废弃(2026-08-29,D9)**:Phase 1–3.5 的产出作为"流程/工具链演练"保留(压缩策略开关、CompactionEvent 采集、A+B+C 打分框架可复用),但其评估结论**不作能力分依据**(数据不可信,GIGO)。换基准后重做数据接入与评估。
- [x] **Phase 1 数据与环境(2026-08-29)**:LoCoBench-Agent clone 到 `/tmp/LoCoBench-Agent`(commit `2ab9218`,工具目录不入仓库);venv 装依赖(requirements 去 lighteval/boto3 + tiktoken + psutil + gdown);`data.zip` 1.27GB 经代理 gdown 下载并解压(4.6GB,1000 项目 / 8000 场景 / 8000 已转换 agent 场景);`convert-scenarios --limit 5` 验证通过(全 8000 走缓存,0 失败)。
- [x] **Phase 1 最小闭环跑通(2026-08-29)**:`benchmark/locobench/` 初版 driver 就绪(`lanscoder_agent.py` / `tool_mapping.py` / `driver.py` / `README.md`)。1 个 easy 场景(`php_api_rest_easy_078_architectural_understanding_easy_01`,19K context)由 `LansCoderAgent` + deepseek-v4-flash 执行 **3 turns**,产出 `AgentEvaluationResults`:**overall 0.704 / LCBA-Comp 0.74 / LCBA-Eff 0.65**,session_status=completed,0 error,59 条工具调用(6 类 LoCoBench 工具 + LansCoder `read_memory`),provider 真实 usage 共 131,327 tokens,**未触发压缩**(19K 场景 vs 1M 窗口,符合预期)。产物在 `benchmark/runs/locobench/smoke-easy-1/`(gitignored)。
- [x] **Phase 1 坑(已修/已记录)**:① harness 场景文件必须嵌套在 `initial_context.project_files`,否则工具工作区为空;② LansCoder 内部 loop 工具调用需从 session store 事件采集(`ChatResponse.tool_calls` 只含末条消息);③ `FinishReason` 是字符串 Literal 不是 enum(首次跑 `'str' object has no attribute 'value'` 中断,已修);④ `--max-turns N` 才是预算上限(阶段循环 success 不满足会跑满回合);⑤ 工具名带 `_copy_<id>` 副本后缀需归一化。
- [x] **Phase 2 driver 完善(2026-08-29)**:① `create_agent_session` 新增 `compaction_strategy` 参数(no_compact / l1_l2 / l1_l2_l3,默认 l1_l2_l3 保持全量现状;`ContextWindowManager` 增加 `CompactionStrategy`,no_compact 直接跳过、l1_l2 规则压不达标时走非 LLM 硬截断兜底不调 L3),driver 新增 `--compaction-strategy`;② `benchmark/locobench/compaction_capture.py`(纯函数事件归一化:before/after tokens、level_metrics→L1/L2 hit、fallback_steps→硬截断标记)+ `analyze.py`(聚合:三套 token 口径 + CompactionEvent 统计,driver 收尾产出 `analysis.json`);③ per-turn 三口径分开标注(harness 启发式 `context_tokens` / provider 真实 usage / lanscoder `chars/4` 估算),失败回合也落 turn_stats(不丢数据)。
- [x] **Phase 3.5 打分链路(2026-08-29)**:`scoring.py`(A 反引号命中率 + C=LCBA-Comp + 加权 0.05/0.65/0.30)+ `judge.py`(B 层,LLM-as-judge 结构化 JSON,key 只从 env 读)实现并有 11 个单测。A+C 对 3 run 出分;**B 层临时用 deepseek-v4-flash 占位跑通**(模拟,正式 judge 待 DashScope 充值后换 qwen3.7-plus):easy-2 quality 0.40 / hard-1 0.45 / expert-1 0.30。临时口径总分:easy-2 0.5447 / hard-1 0.5185 / expert-1 0.4181。关键发现:expert 场景 judge correctness=0.0(agent 根因找错,ground truth 为 module_62 `EventStandardizerV3`,agent 误判 module_21),而 harness LCBA-Comp 0.744 因关键词命中掩盖了错误→ 证实 B 层语义判断的必要性(record.md O9)。
- [x] **Phase 3 hard/expert 冒烟(2026-08-29)**:各 1 个 code-comprehension 场景(200K 窗口、minimal 初始上下文、max 6 turns、策略 l1_l2_l3)——**hard**(`c_desktop_development_expert_021_code_comprehension_hard_01`,1.28M chars):overall 0.68 / comp 0.73 / eff 0.60,6 turns 0 error,**6 次压缩全部触发**(before 167–176K → after 24–113K,target 133.9K),L1 hit 6/6、L2 hit 6/6、**L3 hit 0、硬截断 0**(全部 stopped_at=l2,规则压单独达标);**expert**(`python_data_streaming_expert_085_code_comprehension_expert_01`,1.24M chars):overall 0.67 / comp 0.74 / eff 0.56,6 turns 0 error,**6 次压缩**(before 168–181K → after 36–117K),L1 hit 5/6、L2 hit 6/6、L3 hit 0、硬截断 0。CompactionEvent 采集(hit rate/水位/前后 tokens)端到端验证通过。产物在 `benchmark/runs/locobench/smoke-hard-1/`、`smoke-expert-1/`(gitignored)。

## Pending


- **Phase 4(下一步)**:策略 A/B(no-compact / L1+L2 / L1+L2+L3)+ 分析脚本(上下文规模-指标曲线、压缩行为统计);注意 Phase 3 发现——原生 1M/1.5M 窗口下 chars/4 不触发(见 record.md O6),A/B 需固定窗口(建议 200K)并标注口径。
- **Phase 5**:`benchmark/locobench/README.md` 复现文档完善;更新 `record.md`(补 Phase 1 评估观察);全量门禁(SC-6)。

## Next step

**新基准最小 task 已通过(reward 1.0)。下一步(不着急放大)**:① 与用户敲定**上下文压缩基准的打分方案**(候选:正确性=SWE-bench verifier pass/fail(客观),不做加权合成;报告 pass@1 + 每组 token/压缩行为/成本;不引入 LLM judge);② 给 Harbor 适配器补 `--artifact` 抓 session + 透传 `context_window`/`compaction_strategy`(A/B 用 200K);③ 用户批准后再小批量(5–10 题)三组 A/B。**
**Phase 3 已通过**;**Phase 3.5 进行中**(2026-08-29):A/B/C 打分链路已跑通(权重 0.05/0.65/0.30),B 层已用 deepseek-v4-flash **临时占位**跑通 3 run(临时口径总分 easy 0.5447 / hard 0.5185 / expert 0.4181;expert judge correctness=0,harness 关键词分 0.744 掩盖了根因错误→ O9)。**下一步**:等 DashScope 充值后换 `qwen3.7-plus` 重出正式 B 与总分(当前 B 为占位,不可作正式结论),随后进入 **Phase 4** 三组策略 A/B。

## Verification

- [ ] `node .ai-team/check.mjs --base origin/main` → valid
- [ ] `node .ai-team/session.mjs validate` → valid(private sessions 已启用)
- [x] SC-1:1 个 easy 场景跑通并出分(Phase 1 overall 0.704;Phase 2 复跑 overall 0.76 / comp 0.83 / eff 0.65,3 turns)
- [x] SC-2:hard + expert 各 1 个跑通(overall 0.68 / 0.67,各 6 turns 0 error,压缩 6+6 次全部触发,见 Completed)
- [ ] SC-3:三组策略对比产出(Phase 4)
- [x] `pytest`(1723 passed, 2 skipped)/ `ruff check lanscoder tests benchmark/locobench` 全绿(核心仅新增默认不变策略参数,未改语义)

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-004 **active**——LoCoBench-Agent 接入(纯测量)。**Phase 1/2 完成**。**Phase 3 完成**(2026-08-29):hard + expert 各 1 个冒烟(SC-2 ✅)——200K 窗口下各 6 次压缩全部触发且 L1+L2 单独达标(L3/硬截断 0),CompactionEvent 采集端到端验证;原生 1M/1.5M 窗口 chars/4 不触发(O6,Phase 4 需固定窗口)。下一步 **Phase 4**:三组策略 A/B 对比 + 上下文规模-指标曲线。
