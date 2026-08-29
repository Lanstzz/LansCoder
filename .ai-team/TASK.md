# Current Task

- ID: `TASK-002`
- Title: `SDK 硬化:L1 session-free(P2)、LlmTransport 传输协议(P3)、契约与文档(P1)`
- Status: `planning`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

让 `lanscoder.core` 成为稳定、可对外发布的 SDK 面(参考 pi 的发布形态,范围 P0-P3,不做 P4):

- **P2 L1 真 session-free**: `agent_loop` 不再写盘(temp 目录 → `InMemorySessionStore`),保留工具多轮往返,不承担权限/守卫;流式自动探测 + `use_streaming` 覆盖。
- **P3 传输窄协议化**: `LlmTransport` Protocol(复用 providers 类型),`LoopConfig.provider` 类型改为它;`AgentSessionHandle` 去掉 `agent`,只留 `session + runner`。
- **P1 契约固化**: `py.typed` + 契约测试 + SDK 文档/headless 示例 + API 版本策略,把最终形态钉死。
- P0(删 `app/runtime.py` shim)已由用户提交 `3fab751`,待合入 main。

## Acceptance scenarios

- [ ] **SC-1 (P2 不落盘)**: Given `agent_loop` 改用内存会话,When 运行 L1 且断言无临时目录/文件残留,Then 不落盘、事件序列不变。证据: 新增测试 + 既有 core L1 测试回归。
- [ ] **SC-2 (P2 流式三态)**: Given `LoopConfig.use_streaming` 三态,When 分别运行 L1,Then None=按 `capabilities.supports_streaming` 自动、True=强制流式(有 `MessageUpdateEvent`)、False=强制非流式(只有 `MessageEndEvent`)。
- [ ] **SC-3 (P2 行为边界)**: Given L1 保留工具多轮往返且 `permission_manager=None`,When 运行工具事件测试,Then 无回归。
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
- **D6** P0 立即提交(用户已提交 `3fab751`,待合入 main)。
- **D7** P1 内容:`py.typed` + 契约测试 + SDK 文档/headless 示例(按 L3 + 每任务短会话驱动形态写)+ API 版本策略;顺序 P0→P2→P3→P1。
- **D8** P4 独立分发包不做;`lanscoder.core` 文档化为 SDK 入口。

## Completed

- 2026-08-28/29 SDK 讨论拍板 D1-D8(记录于本 TASK 与 `docs/superpowers/specs/2026-08-29-sdk-hardening-design.md`)。
- P0 已由用户提交: `3fab751`(删 `app/runtime.py` shim,统一 `core.runtime` 导入)— 待合入 main。
- 本 spec(Task 0)为当前 PR 内容。

## Pending

- [ ] 评审并合并本 spec PR(Task 0)。
- [ ] P0 PR(`3fab751`)合入 main。
- [ ] Step 1(P2):`InMemorySessionStore` + L1 内存会话 + 流式三态。
- [ ] Step 2(P3+D2):`LlmTransport` Protocol + handle 去 `agent`。
- [ ] Step 3(P1):`py.typed` + 契约测试 + SDK 文档/示例 + `__version__`。

## Next step

评审并合并本 spec PR;随后进入 Step 1(P2)实现(每 Step 独立 PR,TDD 先行)。

## Verification

- [ ] 全量 `pytest` 绿(基线 1689 passed)。
- [ ] `node .ai-team/check.mjs --base origin/main` 通过。
- [ ] `ruff check lanscoder tests` 通过。
- [ ] SC-1..SC-8 以真实命令退出码记录。

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-001 闭环后启动 TASK-002(SDK 硬化,P0-P3);D1-D8 已拍板(含推翻 P2 原方案 B 改选 A、handle 去 agent、保守 LlmTransport);P0 已提交 `3fab751` 待合入 main;当前为 Task 0(spec + TASK,纯文档),spec 评审通过后按 Step 1→2→3 实现。
