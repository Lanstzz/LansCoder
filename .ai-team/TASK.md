# Current Task

- ID: `TASK-005`
- Title: `eval_harness：本地 trace、回放与 Agent 回归评测体系`
- Status: `planning`
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

- [ ] **SC-1 (统一入口)**：Given `eval_harness` 已初始化，When 运行一个 offline case，Then 不访问网络，当前 runtime 完成一次执行并产出 `trace.jsonl`、`scorecard.json` 和 artifacts。
- [ ] **SC-2 (Trace 完整性)**：Given offline fresh trace，Then 包含脱敏输入、case/config/runtime identity、provider 交互、工具生命周期、异常/恢复、文件产物、最终交付和 trace integrity 信息。
- [ ] **SC-3 (Golden 回归)**：Given 至少 10 个人工微型 golden case，When 重复运行，Then canonicalized trace 的关键事件和 verifier 结果稳定，时间戳与随机 ID 不造成误报。
- [ ] **SC-4 (历史回放)**：Given 一份已有 session/Harbor trace，When 执行脱敏 case extractor，Then 产出可审阅的 replay case，并能驱动当前 runtime 生成 fresh trace。
- [ ] **SC-5 (异常恢复)**：Given provider malformed response、tool failure、tool timeout、取消和 resume 等故障注入，Then runtime 不产生未结算生命周期，恢复行为由 verifier 明确判定。
- [ ] **SC-6 (产物与交付)**：Given fixture case，Then verifier 能检查创建/修改/删除文件、diff、测试结果、禁止路径和最终交付状态。
- [ ] **SC-7 (安全脱敏)**：Given 含 secret、绝对路径和私有输入的 trace，Then 长期 portable trace 只保留脱敏值；原始内容仅存在本地加密 capsule，不能进入 Git 或 scorecard。
- [ ] **SC-8 (Scorecard)**：Given trace 和 verifier 输出，Then 生成机器可读 scorecard，区分硬门禁与性能指标，并支持与基线比较。
- [ ] **SC-9 (Red-team 基础集)**：Given 恶意/异常 provider 输出、超大工具结果、重复结果和越权路径，Then trace 不泄密、runtime 不崩溃、失败原因可归类。
- [ ] **SC-10 (文档与复现)**：Given 新用户只阅读根 `README.md` 和 `eval_harness/README.md`，Then 能理解评测层边界、安装依赖、运行 offline/golden/replay/Harbor case、查看 trace/scorecard，并按文档复现实例。
- [ ] **SC-11 (门禁)**：When 运行 `pytest`、`ruff check .`、`node .ai-team/check.mjs --base origin/main` 和私有 session 校验，Then 全绿。

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
- **D8 (用户已确认，待实施)**：历史 case 采用 portable manifest + local encrypted capsule 双层存储；portable 部分保留稳定 hash 占位符以支持关联，不提供原文恢复能力。
- **D9 (用户已确认，待实施)**：文档是重构交付的一部分：更新根 `README.md` 的 benchmark 入口和状态，新增完整 `eval_harness/README.md`，并迁移/重写 Harbor adapter 文档；文档中的命令、路径、参数和 trace schema 必须与实现一致。

## Completed

- [x] TASK-004 已由用户决定取消；LoCoBench 的 adapter、分析器、专属测试和运行文档已移除，历史教训和观测保留为不可执行证据。
- [x] Harbor + SWE-bench Pro adapter 已完成最小链路验证：qutebrowser reward 1.0（21/21），vuls reward 1.0（77/77）；session JSONL 可收集。
- [x] `--benchmark` 已与 SWE-lite 解耦，使用独立 `120 tools / 120 provider calls / 3600 seconds` 预设，三项限额可逐项覆盖；未接线的 `summary()` 预设已删除。
- [x] 当前 runtime 已有 `JsonlSessionStore`、工具生命周期事件、异常/中断状态、CompactionEvent 和 Harbor session 导出能力，可作为 trace 采集底座。
- [x] 设计决策已确认：长期 trace 脱敏、原始内容本地加密、第一阶段完全离线、人工微型 fixture 作为 canonical golden。
- [x] 已确认文档交付范围：根 README 更新 + `eval_harness/README.md` 新 benchmark 文档 + Harbor adapter 文档迁移/重写。

## Pending

- [ ] 创建 `eval_harness` 包和统一 CLI，迁移 `benchmark/harbor` adapter 入口。
- [ ] 定义 trace/case/scorecard schema、版本策略、canonicalization 和脱敏规则。
- [ ] 实现 scripted provider、offline runner 和 fresh trace recorder。
- [ ] 编写首批人工微型项目 fixture、provider tapes 和至少 10 个 golden case。
- [ ] 实现 trace、artifact、recovery、security、delivery verifier 与机器可读 scorecard。
- [ ] 实现历史 session/Harbor trace 的脱敏 case extractor 和 local encrypted capsule 约定。
- [ ] 补充 red-team case；之后再设计 live model canary 和 Harbor regression matrix。
- [ ] 更新根 `README.md`，新增 `eval_harness/README.md`，并迁移/重写 Harbor 使用与复现文档。

## Next step

先冻结 TASK-005 的 schema 与目录设计，然后创建 `eval_harness` 最小包、offline scripted provider、一个人工 fixture 和一个端到端 golden case，证明“case → current runtime → fresh trace → verifier → scorecard”闭环；同步维护根 README 和 `eval_harness/README.md`，确保第一条可复现命令从文档开始就成立；全程不访问网络、不改 AgentLoop 回合语义。

## Verification

- [ ] `pytest` → TASK-005 实现后重新运行
- [ ] `ruff check .` → TASK-005 实现后重新运行
- [ ] `node .ai-team/check.mjs --base origin/main` → valid
- [ ] `node .ai-team/session.mjs validate` → valid（private sessions 已启用）
- [ ] offline smoke：明确证明无网络访问并产出 trace / scorecard / artifacts
- [ ] golden replay：同一 case 重跑后的 canonicalized 关键事件稳定
- [ ] 文档复现检查：按根 README 和 `eval_harness/README.md` 的命令完成 offline smoke，并核对文档路径/参数与实现一致

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-004 已取消，当前进入 TASK-005 planning。目标是将 benchmark 重构为 `eval_harness`，建立脱敏长期 trace、offline golden、历史 replay、deterministic verifier、scorecard、red-team 和 canary 分层，并同步更新根 README、补齐 `eval_harness/README.md`、迁移/重写 Harbor 文档。用户已确认长期 trace 脱敏、原始内容只本地加密、第一阶段完全离线、人工微型 fixture 作为 canonical golden；历史 trace 作为第二阶段 regression case。下一步实现 schema、最小 offline 闭环和对应复现文档。历史工作区存在未提交改动，继续实施前需保留并审阅。
