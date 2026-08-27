# Project Context

This file contains stable facts shared by every developer and AI. Change it only when the project direction or architecture changes.

## Goal

LansCoder 是一个本地 Python coding agent,带 Textual TUI。当前架构目标:让核心能力像 pi agent 一样三层解耦——L1 `agent_loop`(裸循环)、L2 `Agent`(有状态 wrapper)、L3 `create_agent_session`(完整 coding agent)——三层都能在完全不 import / 初始化 TUI 的情况下使用。`lanscoder/core/` 成为唯一装配源,TUI 只是订阅者之一。

## Scope

- In scope:
  - `lanscoder/core/` 独立包承载 L1/L2/L3 公共 API 与装配根。
  - 装配根从 `app/` 上提到 core,`app/factory.py` 改为消费 core 装配。
  - 依赖方向与层边界约束纳入自动化测试。
- Out of scope:
  - 不重写 `agent/` 会话绑定引擎(`AgentLoop` / `AgentSession`)的回合语义;`agent/` 保持现状,服务 L3 内部。
  - 不改动 providers / context / permissions / tools 的既有职责。
  - 不改变 TUI 行为。

## Architecture

- 终态分层:`providers ← context ← agent ← core ← app ← cli/tests`,新增边仅 `app → core`。
- `lanscoder/core/` = 唯一装配源,承载:
  - `runtime.py`:装配根(`register_loop_tools` / `create_agent_loop` / `CurrentSessionState` / `AgentChatRunner` 及模块级辅助函数,自 `app/runtime.py` 迁入)。
  - `create_agent_session`:headless 唯一装配源(provider + session + 工具 + context 管理器 + runner)。
  - L1 `agent_loop`(无状态,`AsyncIterator[AgentEvent]`)与 L2 `Agent`(`subscribe / prompt / steer / follow_up / abort`)。
  - 消息/事件模型(`LoopMessage` + 10 种 `AgentEvent`)+ `convert_to_llm` 桥接到 provider `ChatMessage`。
- `lanscoder/agent/` = 会话绑定引擎,不 import core,服务 L3 内部。
- 权威文档:`docs/architecture/`、`docs/superpowers/specs/2026-08-27-core-three-layer-decoupling-design.md`。

## Invariants

- **`core` 永不 import `app`**;**`agent` 永不 import `core`**;不产生新依赖环。
- 仓库已有两条"下层引用上层"的惰性边(session 服务 → agent.session、context/store → session.index)保持现状,本次不动。
- 永不提交凭据、私有源码副本、系统/开发者提示、原始工具输出、思维链。
- 仅当仓库为 private 且 `.ai-team/session-policy.json` 显式开启时,才可逐字记录原始用户提交;session 文件是低优先级证据。
- 保留现有行为,除非活动任务显式改变;测试与 CI 决定可观察行为。
- 同一任务同一时刻只有一个写入者;代码与 `.ai-team/TASK.md` 同 PR 合入。

## Commands

- Install: `pip install -e ".[dev]"`
- Test: `pytest`
- Verify: `pytest && node .ai-team/check.mjs --base origin/main && ruff check lanscoder tests`
