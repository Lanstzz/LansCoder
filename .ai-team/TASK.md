# Current Task

- ID: `TASK-005`
- Title: `eval_harness：本地 trace、回放与 Agent 回归评测体系`
- Status: `handoff`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

将当前仅有 Harbor adapter 的 benchmark 层重构为统一的 `eval_harness`，建立可离线运行、可回放、可验证、可长期回归的 Agent 评测体系。

每次运行都应生成可审计的 fresh trace，记录脱敏用户输入、provider 响应、工具调用、工具异常与恢复、文件产物、上下文压缩、最终交付和 verifier 结果；系统应支持从历史 trace 抽取 replay case，重新驱动当前 runtime，并通过 verifier / scorecard 判断行为是否回归。

TASK-004 已取消。其 LoCoBench 评测因数据质量问题废弃；已有 Harbor + SWE-bench Pro 冒烟结果、适配器修复和经验记录保留为历史证据，不再作为新的 harness 设计约束。`benchmark/harbor/` 迁移为 `eval_harness/harbor/`，Harbor 只是外部执行 adapter，不再代表整个评测层。

## Scope

- `eval_harness/` 统一承载 case manifest、trace schema、脱敏、录制、回放、verifier、scorecard 和 Harbor adapter。
- 重构完成后同步更新项目根 `README.md`，并补齐 `eval_harness/README.md` 作为 benchmark/evaluation 使用文档；原 `benchmark/harbor/README.md` 的有效内容迁移、改写并与新入口保持一致。
- 第一阶段完全离线：使用 scripted provider 和小型人工 fixture，不访问网络、不调用真实模型。
- 第二阶段从已有 session / Harbor trace 抽取脱敏 replay case，重跑当前 runtime 生成 fresh trace。
- 第三阶段加入 red-team 异常注入和真实模型 canary；Harbor/SWE-bench Pro 作为外部 regression/canary 来源。
- `lanscoder/` 继续作为被测 runtime，不 import `eval_harness`；除稳定 observer / event 接口外不引入 benchmark 依赖。

Out of scope:

- 不在本任务重写 AgentLoop、ContextWindowManager 或工具语义。
- 不把真实 Harbor 大仓库直接作为第一阶段 golden fixture。
- 不把未脱敏用户输入、API key、系统提示、私有源码或大体积数据提交到 Git。

## Architecture

```text
case manifest + fixture
          ↓
offline replay / live runner
          ↓
current LansCoder runtime
          ↓
canonical fresh trace
          ↓
trace / artifact / recovery / security verifier
          ↓
scorecard + regression report
```

建议目录：

```text
eval_harness/
  schema/       # trace、case、scorecard 数据模型
  trace/        # recorder、redaction、canonicalize
  replay/       # runner、scripted provider、历史 trace 抽取
  verify/       # trace、artifact、recovery、security verifier
  fixtures/     # 小型项目与 provider tapes
  cases/        # offline、golden、regression、redteam、canary
  harbor/       # Harbor adapter
  cli.py
```

Trace 与 case 分离：case 是可执行输入和断言，trace 是本次运行的事实证据；同一 case 可以生成多份 fresh trace。

支持两种主要回放：

- `interaction_replay`：固定 provider response tape，验证当前 runtime 的工具、状态、异常恢复、压缩和 trace 行为。
- `fresh_model`：重新调用真实模型，验证 Agent 能力；结果只做阈值和统计比较，不做全文字节比较。

## Acceptance scenarios

- [x] **SC-1 (统一入口)**：Given `eval_harness` 已初始化，When 运行一个 offline case，Then 不访问网络，当前 runtime 完成一次执行并产出 `trace.jsonl`、`scorecard.json` 和 artifacts。
- [x] **SC-2 (Trace 完整性)**：Given offline fresh trace，Then 包含脱敏输入、case/config/runtime identity、provider 交互、工具生命周期、异常/恢复、文件产物、最终交付和 trace integrity 信息。
- [x] **SC-3 (Golden 回归)**：Given 至少 10 个人工微型 golden case，When 重复运行，Then canonicalized trace 的关键事件和 verifier 结果稳定，时间戳与随机 ID 不造成误报。
- [x] **SC-4 (历史回放)**：Given 一份已有 session/Harbor trace，When 执行脱敏 case extractor，Then 产出可审阅的 replay case，并能驱动当前 runtime 生成 fresh trace。
- [x] **SC-5 (异常恢复)**：Given provider malformed response、tool failure、tool timeout、取消和 resume 等故障注入，Then runtime 不产生未结算生命周期，恢复行为由 verifier 明确判定。
- [x] **SC-6 (产物与交付)**：Given fixture case，Then verifier 能检查创建/修改/删除文件、diff、测试结果、禁止路径和最终交付状态。
- [x] **SC-7 (安全脱敏)**：Given 含 secret、绝对路径和私有输入的 trace，Then 长期 portable trace 只保留脱敏值；原始内容仅存在本地加密 capsule，不能进入 Git 或 scorecard。
- [x] **SC-8 (Scorecard)**：Given trace 和 verifier 输出，Then 生成机器可读 scorecard，区分硬门禁与性能指标，并支持与基线比较。
- [x] **SC-9 (Red-team 基础集)**：Given 恶意/异常 provider 输出、超大工具结果、重复结果和越权路径，Then trace 不泄密、runtime 不崩溃、失败原因可归类。
- [x] **SC-10 (文档与复现)**：Given 新用户只阅读根 `README.md` 和 `eval_harness/README.md`，Then 能理解评测层边界、安装依赖、运行 offline/golden/replay/Harbor case、查看 trace/scorecard，并按文档复现实例。
- [x] **SC-11 (门禁)**：When 运行 `pytest`、`ruff check .`、`node .ai-team/check.mjs --base origin/main` 和私有 session 校验，Then 全绿。

## Invariants

- `lanscoder/core`、`lanscoder/agent` 不 import `eval_harness`；harness 通过 runtime 的公开装配、observer、session store 和事件接口采集事实。
- 第一阶段完全离线，脚本和测试不得隐式创建网络 provider 或访问外部服务。
- 长期 portable trace 默认只保存脱敏输入；原始输入、私有源码和未脱敏工具结果只允许存放在仓库外的本地加密 capsule。
- Trace 事件必须有稳定序号和 schema version；时间戳、随机 ID、provider request ID 在 canonicalize 时不可导致 golden 误报。
- 工具生命周期必须闭合；异常、取消、超时和 resume 必须有可验证的状态迁移。
- Artifact verifier 不得读取 verifier 隐藏信息来污染 Agent 输入；case、runtime 和 verifier 的边界必须清晰。
- Golden fixture 保持小型、人工可读、可审查；不提交 Harbor 大仓库、数据集缓存或大体积 transcript。
- deterministic verifier 优先于 LLM judge；真实模型结果只允许进入 regression/canary 统计，不作为 offline 门禁依赖。
- 同一任务同一时刻只有一个写入者；代码、case、测试和 `.ai-team/TASK.md` 在同一 PR 中同步。

## Decisions

- **D1 (用户已定)**：TASK-004 取消，LoCoBench 可执行集成删除；已有结果只作为历史证据。新的统一根目录为 `eval_harness/`，Harbor 迁移为其 adapter。
- **D2 (用户已定)**：长期 trace 默认保存脱敏输入；原始内容只允许本地加密存储，不能进入 Git、portable replay case 或 scorecard。
- **D3 (用户已定)**：第一阶段完全离线，先用 scripted provider 和本地 fixture 跑通完整 trace → verifier → scorecard 链路，再接真实模型。
- **D4 (用户已确认，待实施)**：第一阶段 canonical golden 使用人工编写的小型 fixture 仓库；历史 trace 抽取作为第二阶段 regression case；Harbor 任务仅用于后续 regression/canary。
- **D5 (用户已确认，待实施)**：fixture 首批覆盖无工具完成、读改测、多文件修改、provider malformed response、tool failure/retry、tool timeout、interrupt/resume、重复结果、越权路径和压缩事件。
- **D6 (用户已确认，待实施)**：提供 `interaction_replay` 与 `fresh_model` 两种模式；前者做确定性 runtime 回归，后者只做 live 能力与阈值统计。
- **D7 (用户已确认，待实施)**：scorecard 采用 trace、artifact、recovery、security、delivery 五类硬门禁，provider calls、tool calls、elapsed、token、context 和 compaction 作为独立指标。
- **D8 (用户已确认，已实施)**：历史 case 采用 portable manifest + local encrypted capsule 双层存储；portable 部分保留稳定 hash 占位符以支持关联，不提供原文恢复能力。
- **D9 (用户已确认，已实施)**：文档是重构交付的一部分：更新根 `README.md` 的 benchmark 入口和状态，新增完整 `eval_harness/README.md`，并迁移/重写 Harbor adapter 文档；文档中的命令、路径、参数和 trace schema 必须与实现一致。
- **D10（本 checkpoint 实施）**：v1 portable trace 对 system prompt、工具描述、工具参数与工具结果正文一律不保留原文；使用 redacted placeholder、字段摘要和稳定 SHA-256 指纹，保留 verifier 所需的生命周期、状态与完整性事实。
- **D11（本 checkpoint 实施）**：真实 resume/compaction case 显式声明 `runtime: "session"`；runner 只持久化到 fresh run 的临时 runtime 目录并在投影 compaction facts 后删除，避免 session 原始内容进入 portable trace。重复 tool ID 作为 red-team 负例保留失败 scorecard，但必须输出可分类的 recovery evidence。
- **D12（本 checkpoint 实施）**：live model canary 使用与离线相同的五类 hard gate，但对真实模型只做阈值/统计比较，不做全文 trace golden；Harbor 按 provider/model/config、dataset/task set、repetition、repair policy 和环境版本组成显式 matrix，并将 agent failure 与 provider/infra/timeout/missing-result 分开统计。
- **D13（本 checkpoint 实施）**：`fresh_model` 由 eval harness 直接装配 LansCoder provider，使用 normal model catalog、`api_key`/`api_key_env` 和独立 canary config；Harbor 不作为其依赖，`canary` 命令仅批量编排 direct fresh-model runs。

## Completed

- [x] TASK-004 已由用户决定取消；LoCoBench 的 adapter、分析器、专属测试和运行文档已移除，历史教训和观测保留为不可执行证据。
- [x] Harbor + SWE-bench Pro adapter 已完成最小链路验证：qutebrowser reward 1.0（21/21），vuls reward 1.0（77/77）；session JSONL 可收集。
- [x] `--benchmark` 已与 SWE-lite 解耦，使用独立 `120 tools / 120 provider calls / 3600 seconds` 预设，三项限额可逐项覆盖；未接线的 `summary()` 预设已删除。
- [x] 当前 runtime 已有 `JsonlSessionStore`、工具生命周期事件、异常/中断状态、CompactionEvent 和 Harbor session 导出能力，可作为 trace 采集底座。
- [x] 设计决策已确认：长期 trace 脱敏、原始内容本地加密、第一阶段完全离线、人工微型 fixture 作为 canonical golden。
- [x] 已确认文档交付范围：根 README 更新 + `eval_harness/README.md` 新 benchmark 文档 + Harbor adapter 文档迁移/重写。
- [x] 第一阶段最小闭环已落地：`eval_harness` v1 schema、JSON CLI、scripted provider、网络拒绝器、fresh JSONL recorder、trace/artifact/recovery/security/delivery verifier、scorecard、一个人工 fixture/case 与测试均通过公开 L1 `lanscoder.core.agent_loop` 接口工作。
- [x] Portable trace 默认不保存 system prompt、工具描述、工具参数或工具结果正文；这些值仅留 redacted placeholder、稳定指纹或字段摘要，且 trace integrity 使用稳定序号与 SHA-256 footer。
- [x] Harbor adapter 已从 `benchmark/harbor/` 迁移到 `eval_harness/harbor/`；根 README、harness README 和 Harbor 命令已改用统一入口。
- [x] verifier 已覆盖 provider error 分类、tool timeout/failure、interrupt 后的错误结束与中断结算、artifact diff 路径安全和 compaction 事件结构；scorecard 新增 provider/tool/recovery/compaction 指标。
- [x] 增加 deterministic fault probe manifest 字段与 provider tape fault；CLI 支持 `run --baseline` 和独立 `compare` 命令，丢失已通过的 hard gate 会报告为 regression。
- [x] eval manifest 增加 `runtime: "session"`、`resume_after_interrupt`、`warmup_prompts`、context window/compaction strategy 和可控工具结果大小；runner 通过公开 `create_agent_session` 真实采集持久化 resume、取消后的生命周期与 L3 compaction facts，不把 runtime session 原始 JSONL 留在 portable 输出。
- [x] 增加 red-team 基础集：秘密-bearing provider output、100K 工具结果、重复 tool ID 和越权路径；portable trace 只保存脱敏值/摘要/指纹，重复调用由 recovery gate 的 `duplicate_tool_ids` 明确分类。
- [x] artifact/delivery verifier 支持 manifest 精确声明 `created`/`modified`/`deleted` diff、禁止路径和 delivery completion；新增 `delete_file` 仅在 case 明确使用时启用，并用 fixture case 覆盖删除及完整 diff hard gate。
- [x] trace verifier 已补齐 case/config/runtime identity、脱敏输入指纹、provider request/outcome 配对、provider tool call 与工具生命周期闭合、异常/恢复摘要、最终交付一致性、完成计数和 integrity footer 校验；provider request 的工具参数 schema 改为占位符加稳定指纹，并增加对应负例测试。

## Pending

- [x] 创建 `eval_harness` 包和统一 CLI，迁移 `benchmark/harbor` adapter 入口。
- [x] 冻结第一版 trace/case/scorecard schema、版本策略、canonicalization 和脱敏规则。
- [x] 实现 scripted provider、offline runner 和 fresh trace recorder。
- [x] 将人工微型项目 fixture、provider tapes 和 golden case 扩展到至少 10 个；当前离线目录包含 11 个可执行 deterministic case，并有批量 scorecard 回归测试。
- [x] 将 artifact、recovery、security、delivery verifier 与机器可读 scorecard 扩展到完整 artifact diff、真实异常、取消、恢复、压缩和基线比较场景；当前已接入真实 session resume/L3 compaction 采集。
- [x] 补齐 trace verifier 对 config/runtime identity、provider/tool lifecycle、异常恢复、最终交付和 integrity 的完整覆盖，并继续保持 portable trace 脱敏。
- [x] 实现历史 session/Harbor trace 的脱敏 case extractor 和 local encrypted capsule 约定。
- [x] 补充 red-team case；之后再设计 live model canary 和 Harbor regression matrix。
- [x] 设计 live model canary 和 Harbor regression matrix；设计文档明确 `fresh_model` 后续 runner 的输入、五类 hard gate、统计/基线规则、Harbor H0-H4 cells、结果分类和证据留存边界。
- [x] 更新根 `README.md`，新增 `eval_harness/README.md`，并迁移/重写 Harbor 使用与复现文档。
- [x] 扩展 deterministic offline golden 集：补充无工具完成、单/多文件写入、fixture 修改/覆盖、失败后重试、重复写入、嵌套 Unicode、多轮工具调用和越权路径拒绝等 case；删除仓库工作区中的历史 `benchmark/` 运行产物目录。
- [x] 实现历史 session/Harbor trace 脱敏 case extractor 与 local encrypted capsule：支持 LansCoder session JSONL、fresh trace JSONL、Harbor job 目录，portable manifest 只保留 hash 占位符，运行时显式解密 capsule。

## Completed this checkpoint

- [x] 实现不依赖 Harbor 的 `fresh_model` runner、真实 provider interaction recorder、token usage scorecard、单 case CLI 分派、批量 `canary` 命令和 checked-in canary config；通过 fake provider 集成测试与 DeepSeek 真实 provider smoke 验证。

## Next step

按设计文档运行并维护 live canary 与 Harbor H1-H4 regression matrix；优先固定 provider/model/config lineage 和任务集，再扩大 repetitions。当前 checkpoint 已完成不依赖 Harbor 的 `fresh_model` runner、canary 执行配置、live provider trace 录制、trace verifier 完整覆盖、artifact/delivery 断言、真实 session resume/L3 compaction 与 red-team 基础集；不改 AgentLoop 回合语义。

## Verification

- [x] `pytest` → 1784 passed in 58.42s（2026-08-31，本 checkpoint）
- [x] `ruff check .` → All checks passed（2026-08-31，本 checkpoint）
- [x] `node .ai-team/check.mjs --base ca5ff7431e13efb0df698d2092cc87e8423b6c5d` → valid；functional progress 11/11；本分支基于 PR #27 的 stacked 提交，故使用实际 merge-base；直接使用 `origin/main` 时因 PR #27 尚未合入而不是当前分支祖先，门禁会拒绝该参数（2026-08-31，本 checkpoint）。
- [x] `node .ai-team/session.mjs validate` → `{ "valid": true, "enabled": true, "errors": [] }`；已审阅现有 session Markdown；本次 hook 未生成新的 TASK-005 session 文件（2026-08-31）
- [x] offline smoke：`venv/bin/python -m eval_harness run --case eval_harness/cases/offline/write_greeting.json --output /private/tmp/lanscoder-eval-smoke-20260830-verified` 通过；五类 gate 全绿，network guard attempts 为 0，并生成 trace / scorecard / artifacts。
- [x] golden replay：`tests/test_eval_harness.py` 两次 fresh run 的 canonical JSON 相同；时间戳、elapsed、trace digest 和随机 message/part ID 不造成误报。
- [x] 文档复现检查：根 README 与 `eval_harness/README.md` 均指向现有命令和路径；CLI smoke 已按 harness README 命令参数实跑。
- [x] focused fault/scorecard tests：`tests/test_eval_harness.py` → 23 passed；CLI baseline run/compare smoke → exit 0，gate regressions 为空（2026-08-30）。
- [x] focused eval tests：`tests/test_eval_harness.py` → 31 passed；覆盖 session resume、真实 L3 compaction、四个 red-team case、created/modified/deleted diff、forbidden path 和 delivery completion。red-team CLI smoke：malicious/oversized/unauthorized exit 0，duplicate exit 1 且 `duplicate_tool_ids` 可见；L3 compaction probe 已改为显式 `prompt_too_long` 注入，避免 CI token 水位差异导致测试不稳定（2026-08-31）。
- [x] trace verifier focused tests：`tests/test_eval_harness.py` → 36 passed；新增 identity 缺失、provider outcome 缺失、tool orphan/lifecycle、final delivery 篡改和 integrity footer 篡改负例；同时验证工具参数 schema 不保留原文（2026-08-31）。
- [x] history extractor/capsule focused tests：`tests/test_eval_extractor.py tests/test_eval_harness.py` → 28 passed；`ruff check eval_harness tests/test_eval_extractor.py` → All checks passed；覆盖 session/trace/Harbor 目录抽取、capsule hydrate/replay、CLI 口令环境变量、错误口令与篡改拒绝（2026-08-30）。
- [x] 本 checkpoint 实际验证：`venv/bin/pytest` → 1784 passed in 59.23s；`venv/bin/ruff check .` → All checks passed；`node .ai-team/check.mjs --base 3cfbce37e1227a2ec9a2193ea7e02be6a57001bf` → valid，functional progress 11/11；`node .ai-team/session.mjs validate` → valid/enabled，无 errors（2026-08-31）。系统解释器直接执行 `pytest` 因环境缺少 `yaml` 在收集阶段失败，项目 `venv` 验证通过。
- [x] fresh model focused tests：`tests/test_eval_live.py tests/test_eval_harness.py tests/test_eval_extractor.py` → 46 passed；覆盖 direct fresh-model trace、token usage、批量 repetitions、config API key env resolution 与 Harbor-free execution（2026-08-31）。
- [x] 真实 provider smoke：`export all_proxy=http://127.0.0.1:7890 && venv/bin/python -m eval_harness canary --config eval_harness/canary.json --project . --output /private/tmp/lanscoder-live-smoke-20260831-01` → exit 0；DeepSeek `deepseek-v4-flash`，1 case/1 repetition，五类 gate 全绿，2 provider calls、1 tool call、0 provider/tool errors、token usage 5059/146/5205；生成 trace、scorecard、artifacts（2026-08-31）。
- [x] 本 checkpoint 最终验证：`venv/bin/pytest` → 1788 passed in 57.34s；`venv/bin/ruff check .` → All checks passed；`git diff --check` → 通过（2026-08-31）。

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: 在既有最小离线闭环上完成 deterministic provider/tool fault probe、interrupt lifecycle verifier、artifact diff 校验、compaction no-op probe、scorecard recovery metrics 与 baseline compare CLI；前一 checkpoint 新增历史 session/trace/Harbor 目录 extractor、hash-only portable manifest、PBKDF2/HMAC 加密 capsule 与显式 hydrate/replay；随后补充真实 session resume/L3 compaction、red-team 基础集、创建/修改/删除/禁止路径和 delivery completion hard gate，并将 compaction probe 改为显式 provider fault 以消除 CI 水位差异。本 checkpoint 补齐 trace verifier 的 identity、provider/tool lifecycle、异常恢复、最终交付和 integrity 覆盖，完成 live model canary 与 Harbor regression matrix 设计，并实现不依赖 Harbor 的 `fresh_model` direct provider runner、真实 interaction recorder、token usage scorecard、单 case/批量 canary CLI 与 config。DeepSeek 真实 smoke、1788 项全量测试、ruff、diff 检查均通过；不改 AgentLoop 回合语义或 core 公共字段。任务状态切回 handoff，下一步是按 H1-H4 执行并维护 live canary/Harbor regression matrix。
