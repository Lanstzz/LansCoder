# Current Task

- ID: `TASK-001`
- Title: `三层解耦 API:core 承载 L1/L2/L3,装配根上提`
- Status: `active`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

让 LansCoder 具备 pi agent 那样的三层解耦 API,三层都能在完全不 import / 初始化 TUI 的情况下使用:

- **L1 `agent_loop`**: 裸循环,无状态,`AsyncIterator[AgentEvent]`,不碰 session / 持久化 / TUI,事件流往外推。
- **L2 `Agent`**: 有状态 wrapper,`subscribe / prompt / steer / follow_up / abort`,内部驱动 L1。
- **L3 `create_agent_session`**: 完整 coding agent(持久化 + 内置工具 + provider + 权限),TUI 只是订阅者之一。

## Acceptance scenarios

- [x] **SC-1 (Step 1 零行为变化)**: Given 装配根从 `app/runtime.py` 原样搬到 `lanscoder/core/runtime.py` 且 `app/runtime.py` 变为 re-export shim,When 运行全量测试与依赖方向/层边界测试,Then 全部绿、行为零变化。证据: `pytest` 1666 passed、`ruff` all checks passed。
- [ ] **SC-2 (Step 2 只加不改)**: Given 新增 `create_agent_session`(headless 唯一装配源:provider + session + 工具 + context 管理器 + runner)及 L1/L2 公共 API,When 运行新增测试与全量回归,Then 旧行为不变、新测试绿。
- [ ] **SC-3 (Step 3 factory 消费 core)**: Given `app/factory.py` 改为消费 `create_agent_session`(模型选择逻辑如 ModelStateStore 保留在 factory,传入选好的 provider),When 运行 factory 测试与 TUI 回归,Then 行为保持、测试绿。
- [x] **SC-4 (依赖方向)**: Given `lanscoder/core` 存在,When 运行 AST 依赖方向扫描与 fresh-interpreter 泄漏检查,Then `core` 永不 import `app`、`agent` 永不 import `core`,且 `tests/test_dependency_directions.py` 与 `tests/test_layer_boundaries.py` 已把 core 纳入约束。证据: 新增 6 个用例全绿(`test_core_runtime_shim.py` 3 + layer boundaries 3 + dependency directions 2)。
- [ ] **SC-5 (无 TUI 可用)**: Given 只 import `lanscoder.core` 的 L1/L2/L3,When 在不初始化 Textual TUI 的环境运行最小 headless smoke test,Then 三层均可正常创建与驱动。

## Invariants

- `core` 永不 import `app`;`agent` 永不 import `core`;不产生新环。
- 终态依赖方向:`providers ← context ← agent ← core ← app ← cli/tests`,新增边仅 `app → core`。
- 保留现有行为,除非本任务显式改变;`agent/` 会话绑定引擎语义不改。
- 已有两条"下层引用上层"的惰性边(session 服务 → agent.session、context/store → session.index)本次不动。
- 测试与 CI 决定可观察行为;AI 自报不算验收。

## Decisions

- **D1** 独立包 `lanscoder/core/` 承载三层 API;`lanscoder/agent/` 保持现状(会话绑定引擎,服务 L3 内部)。
- **D2** L1 消息模型走 pi 式路线 B:core 自建轻量消息类型 + `convert_to_llm` 桥接到 provider 的 `ChatMessage`。命名用 `LoopMessage`(候选 `CoreMessage`,见 spec §开放决策)。
- **D3** L1/L2 事件集照搬 pi 的 10 种 `AgentEvent`;`message_update.assistant_message_event` 复用 provider 的 `ChatStreamEvent`。L3 事件词汇与 L1/L2 不同是 pi 原生设计。
- **D4** 工作流:需求 → spec → TDD → 实现;需求未完全明确前不实现。
- **D5** Q4 拍板:装配根上提到 `lanscoder/core/`,`core` 成为唯一装配源,`app/factory.py` 改为消费 core 装配(选项 b),分三步走,每步测试兜底。

## Completed

- 需求与决策已定稿(决策记录:仓库根 `handoff.md`;spec:`docs/superpowers/specs/2026-08-27-core-three-layer-decoupling-design.md`)。
- 本任务已编码进 `.ai-team/TASK.md`;repo-task-sync skill 已注册到 `~/.codex/skills`;仓库补齐 `AGENTS.md` 与真实 `PROJECT.md`。
- 依赖方向结论已核实(handoff「依赖方向结论」节)。
- **Step 1 完成**: 装配根(`register_loop_tools` / `create_agent_loop` / `CurrentSessionState` / `AgentChatRunner`)原样迁至 `lanscoder/core/runtime.py`(新增 `__all__`),`lanscoder/app/runtime.py` 变 re-export shim;依赖方向/层边界测试纳入 core;新增 shim 防漂移测试。

## Pending

- [ ] 评审并合并本 PR(spec + TASK.md + PROJECT.md + AGENTS.md)。
- [x] Step 1:装配根搬迁到 `lanscoder/core/runtime.py`,`app/runtime.py` 变 re-export shim,依赖/层边界测试纳入 core。
- [ ] Step 2:新增 `create_agent_session` + L1/L2 公共 API + 新测试。
- [ ] Step 3:`app/factory.py` 改为消费 core 装配,行为保持。

## Next step

评审并合并本 Step 1 PR;合并后开始 Step 2:新增 `create_agent_session`(headless 唯一装配源)+ L1 `agent_loop` / L2 `Agent` 公共 API(O1=方案 A,`LoopContext` 显式注入)+ `LoopMessage`/`AgentEvent` 模型 + 新测试。

## Verification

- [x] `pytest` 全量绿: 1666 passed(2026-08-27)。
- [x] `node .ai-team/check.mjs --base origin/main` 通过(Step 1 分支)。
- [x] `ruff check lanscoder tests` 通过。
- [x] SC-1 / SC-4 已以真实命令退出码记录;SC-2 / SC-3 / SC-5 待 Step 2 / Step 3。

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: Task 0(需求+spec)与 Step 1(装配根搬迁+shim+依赖约束)已完成并各自开 PR。下一步: 评审合并 Step 1 PR,然后开始 Step 2(create_agent_session + L1/L2)。
