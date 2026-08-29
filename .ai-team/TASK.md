# Current Task

- ID: `TASK-002`
- Title: `SDK 硬化:L1 session-free(P2)、LlmTransport 传输协议(P3)、契约与文档(P1)`
- Status: `handoff`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

让 `lanscoder.core` 成为稳定、可对外发布的 SDK 面(参考 pi 的发布形态,范围 P0-P3,不做 P4):

- **P2 L1 真 session-free**: `agent_loop` 不再写盘(temp 目录 → `InMemorySessionStore`),保留工具多轮往返,不承担权限/守卫;流式自动探测 + `use_streaming` 覆盖。
- **P3 传输窄协议化**: `LlmTransport` Protocol(复用 providers 类型),`LoopConfig.provider` 类型改为它;`AgentSessionHandle` 去掉 `agent`,只留 `session + runner`。
- **P1 契约固化**: `py.typed` + 契约测试 + SDK 文档/headless 示例 + API 版本策略,把最终形态钉死。
- P0(删 `app/runtime.py` shim):已由 PR #6 合入 main(`5f47888`,2026-08-29)。

## Acceptance scenarios

- [x] **SC-1 (P2 不落盘)**: Given `agent_loop` 改用内存会话,When 运行 L1 且断言无临时目录/文件残留,Then 不落盘、事件序列不变。证据: 新增测试 + 既有 core L1 测试回归。
- [x] **SC-2 (P2 流式三态)**: Given `LoopConfig.use_streaming` 三态,When 分别运行 L1,Then None=按 `capabilities.supports_streaming` 自动、True=强制流式(有 `MessageUpdateEvent`)、False=强制非流式(只有 `MessageEndEvent`)。
- [x] **SC-3 (P2 行为边界)**: Given L1 保留工具多轮往返且 `permission_manager=None`,When 运行工具事件测试,Then 无回归。
- [ ] **SC-4 (P3 传输协议)**: Given `LlmTransport` Protocol,When 用非 `ChatProvider` 的 duck-typed transport 驱动 L1,Then 可跑通;且 `ChatProvider` 结构性满足 Protocol。
- [ ] **SC-5 (P3 handle 瘦身)**: Given `AgentSessionHandle` 去掉 `agent`,When 运行全量回归,Then 绿;L2 `Agent` 独立可用。
- [ ] **SC-6 (P1 契约)**: Given `py.typed` + 契约测试,When 运行,Then `core.__all__` 与签名钉死、泄漏检查仍绿。
- [ ] **SC-7 (P1 文档/示例)**: Given SDK 文档 + headless 示例(L3 + 自定义工具 + set_permission_mode + tool_event_handler 审计 + resume),When 在无 TUI 环境运行,Then 可跑通。
- [ ] **SC-8 (P1 门禁)**: When 运行全量 `pytest` + `node .ai-team/check.mjs --base origin/main` + `ruff check lanscoder tests`,Then 全绿。

## Invariants

- `core` 永不 import `app`;`agent` 永不 import `core`;不产生新环。
- 不重写 `agent/` 会话绑定引擎(`AgentLoop` / `AgentSession`)的回合语义;P2 仅新增 `InMemorySessionStore`,不改引擎。
- 不改动 providers / context / permissions / tools 的既有职责(仅新增 store 子类与 `core/transport.py`)。
- 不改变 TUI 行为。
- 本任务按 spec 评审通过后实现;Task 0(spec + TASK)只写文档,不动代码。

## Decisions

- **D1** P2 选方案 A:复用 `agent/` 引擎 + 内存会话,不重写裸循环(推翻原方案 B;不造第二个循环引擎)。
- **D2** `AgentSessionHandle` 去掉 `agent`,只留 `session + runner`;L2 `Agent` 保持独立(session-free)。
- **D3** P3 保守版 `LlmTransport` Protocol(复用 `providers.types` 类型),`LoopConfig.provider` 字段名不变、类型改为它;不引入 `stream_fn` 主 API。
- **D4** L1 流式:自动探测(`capabilities.supports_streaming`)+ 可选 `use_streaming: bool | None` 覆盖。
- **D5** L1 行为边界:保留工具多轮往返(复用 `ToolExecutor`);不承担权限/守卫/compaction;保留循环级 `limits/context_window/request_options`。
- **D6** P0 立即提交(用户提交 `3fab751`;cherry-pick 到干净分支 `603d806`,PR #6 合入后闭环)。
- **D7** P1 内容:`py.typed` + 契约测试 + SDK 文档/headless 示例(按 L3 + 每任务短会话驱动形态写)+ API 版本策略;顺序 P0→P2→P3→P1。
- **D8** P4 独立分发包不做;`lanscoder.core` 文档化为 SDK 入口。

## Completed

- 2026-08-28/29 SDK 讨论拍板 D1-D8(记录于本 TASK 与 `docs/superpowers/specs/2026-08-29-sdk-hardening-design.md`)。
- Task 0 spec PR #5 已合入 main(`2586642`)。
- P0:用户提交 `3fab751` 后,已 cherry-pick 到基于 main 的干净分支 `codex/p0-drop-app-runtime-shim`(commit `603d806`,15 文件,+14/-54,539 tests passed、ruff clean);由本 PR(#6)合入。
- P0 验证(2026-08-29,分支 `codex/p0-drop-app-runtime-shim`,commit `603d806`+`ed271af`):`pytest` = 1665 passed + 1 skipped;`ruff check lanscoder tests` 全绿;`node .ai-team/check.mjs --base origin/main` = valid(2 commits,20 文件,+553/-81;private sessions 5/closed 3,token coverage 3/5);PR #6 = OPEN / MERGEABLE / CLEAN。
- P0 已合入 main:PR #6 merge commit `5f47888`(2026-08-29)。
- Step 1(P2)实现(分支 `codex/task-002-step1-p2`):新增 `InMemorySessionStore`(`lanscoder/context/store.py`,不建目录、不写盘,复用基类 `_apply_event` 重建);`lanscoder/core/agent_loop.py` 临时目录 → 内存会话 + `use_streaming` 三态(None=自动探测 `capabilities.supports_streaming`、True=强制流式、False=强制非流式);`lanscoder/core/messages.py` `LoopConfig` 新增 `use_streaming: bool | None = None`;零引擎改动。
- Step 1(P2)验证(2026-08-29):新增 7 个测试(SC-1 不落盘 ×2、SC-2 流式三态 ×4、SC-3 工具多轮往返 ×1);`pytest` = 1672 passed + 1 skipped;`ruff check lanscoder tests` 全绿;`node .ai-team/check.mjs --base origin/main` = valid。

## Pending

- [x] 评审并合并本 spec PR(Task 0,#5)。
- [x] P0 PR(#6,`codex/p0-drop-app-runtime-shim`)合入 main(`5f47888`)。
- [x] Step 1(P2):`InMemorySessionStore` + L1 内存会话 + 流式三态(SC-1..SC-3 通过)。
- [ ] Step 2(P3+D2):`LlmTransport` Protocol + handle 去 `agent`。
- [ ] Step 3(P1):`py.typed` + 契约测试 + SDK 文档/示例 + `__version__`。

## Next step

Step 1(P2)已实现并验证(本 PR #7);合并后进入 Step 2(P3+D2):`LlmTransport` Protocol + `AgentSessionHandle` 去 `agent`(每 Step 独立 PR,TDD 先行)。

## Verification

- P0 分支实测(2026-08-29):`pytest` = 1665 passed + 1 skipped;`ruff check lanscoder tests` 通过;`node .ai-team/check.mjs --base origin/main` = valid。
- Step 1 分支实测(2026-08-29):`pytest` = 1672 passed + 1 skipped(含新增 7 个 L1 测试);`ruff check lanscoder tests` 通过;`node .ai-team/check.mjs --base origin/main` = valid。
- [ ] 全量 `pytest` 绿(基线 1689 passed,spec 时点计数;Step 1 分支实测 1672 passed + 1 skipped)。
- [ ] `node .ai-team/check.mjs --base origin/main` 通过。
- [ ] `ruff check lanscoder tests` 通过。
- [ ] SC-1..SC-8 以真实命令退出码记录(SC-1..SC-3 已按真实退出码勾选)。

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-002 启动;D1-D8 已拍板;Task 0 spec(#5)已合入 main;P0 已合入 main(`5f47888`);Step 1(P2)实现与验证完成(`codex/task-002-step1-p2`,pytest 1672 passed + 1 skipped、ruff 全绿、check.mjs valid,SC-1..SC-3 通过);合并本 PR(#7)后进入 Step 2(P3+D2)。
