# 三层解耦 API 设计(spec)

- 日期: 2026-08-27
- 状态: 待评审(Task 0 / TASK-001)
- 上游决策记录: 仓库根 `handoff.md`(D1-D5 已定稿)
- 关联任务: `.ai-team/TASK.md` TASK-001

## 1. 背景与目标

LansCoder 目前的编排装配根(`register_loop_tools` / `create_agent_loop` / `CurrentSessionState` / `AgentChatRunner`)在 `lanscoder/app/runtime.py`,TUI 通过 `setattr` 安装 stream/tool handler。任何想复用核心能力的代码都必须 import/初始化 TUI 相关路径,无法 headless 使用。

目标: 像 pi 一样三层解耦,三层都能在完全不 import / 初始化 TUI 的情况下使用:

| 层 | 形态 | 职责 |
|----|------|------|
| L1 `agent_loop` | 无状态函数,`AsyncIterator[AgentEvent]` | 裸循环,事件流外推,不碰 session / 持久化 / TUI |
| L2 `Agent` | 有状态类 | `subscribe / prompt / steer / follow_up / abort`,内部驱动 L1 |
| L3 `create_agent_session` | 工厂函数 | 完整 coding agent(持久化 + 内置工具 + provider + 权限 + runner),TUI 只是订阅者之一 |

## 2. 参考:pi 的三层结构

- `packages/agent` = `@earendil-works/pi-agent-core`:
  - L1: `agentLoop(prompts, context, config, signal, streamFn)` → `EventStream<AgentEvent, AgentMessage[]>`;`runAgentLoop` 是底层实现,经 `emit` 回调推事件。
  - L2: `Agent` 类 — `subscribe(listener, signal)`, `prompt(input, images?)`, `steer(message)`(当前回合后注入), `followUp(message)`(仅在 agent 本要停止时注入), `abort()`。
  - `AgentEvent` 共 10 种: agent_start / agent_end / turn_start / turn_end / message_start / message_update / message_end / tool_execution_start / tool_execution_update / tool_execution_end。
- `packages/coding-agent` = `@earendil-works/pi-coding-agent`:
  - L3: `createAgentSession(options)` → `{ session, extensionsResult, modelFallbackMessage? }`,内部把持久化、工具、模型运行时、权限/信任组装起来;TUI 只是订阅 session 事件的订阅者之一。

## 3. 目标 API(三层签名)

### 3.1 L1 `agent_loop`

```python
# lanscoder/core/agent_loop.py(目标形态)
async def agent_loop(
    prompts: list[LoopMessage],
    context: LoopContext,        # system_prompt + messages + tools + session_id
    config: LoopConfig,          # model / reasoning / limits / request_options / ...
    signal: CancellationToken | None,
    stream_fn: StreamFn,         # provider 流桥: 产出 ChatStreamEvent
) -> AsyncIterator[AgentEvent]:
    """无状态裸循环: 事件流外推,不碰 session / 持久化 / TUI。"""
```

要点:
- 不接收 `AgentSession` / `CurrentSessionState`,所有输入经参数显式注入。
- 与 pi `runAgentLoop` 对齐: `agent_start → turn_start → message_start/end → (tool_execution_*) * → turn_end → agent_end`。
- 当前 `lanscoder/agent/loop.py::AgentLoop` 是 session 绑定引擎;L1 是其"对 session 透明"的公共面(见 §6 Step 2 与 §9 开放决策)。

### 3.2 L2 `Agent`

```python
# lanscoder/core/agent.py(目标形态)
class Agent:
    def subscribe(self, listener: Callable[[AgentEvent], Awaitable[None] | None]) -> Callable[[], None]: ...
    async def prompt(self, input: str | LoopMessage | list[LoopMessage]) -> None: ...
    def steer(self, message: LoopMessage) -> None: ...      # 当前回合后注入
    def follow_up(self, message: LoopMessage) -> None: ...   # agent 本要停止时注入
    def abort(self) -> None: ...
```

要点:
- 有状态: 内部持有 messages / tools / config,驱动 L1。
- `steer` 与 `follow_up` 语义照搬 pi(steering 队列 / follow-up 队列)。
- `abort` 通过取消信号中断当前 L1 运行。

### 3.3 L3 `create_agent_session`

```python
# lanscoder/core/session.py(目标形态)
def create_agent_session(
    *,
    provider: ChatProvider,
    project_root: str | Path,
    data_root: str | Path | None = None,
    tools: list[Tool] | None = None,        # 缺省 = create_builtin_registry(...)
    context_manager: ContextWindowManager | None = None,
    session_id: str | None = None,
    resume: bool = False,
    limits: AgentLoopLimits | None = None,
    request_options: MainRequestOptions | None = None,
    context_window: int | None = None,
    background_manager: BackgroundJobManager | None = None,
    # ... 其余透传项
) -> AgentSessionHandle:
    """headless 唯一装配源: provider + session + 工具 + context 管理器 + runner。"""
```

要点:
- 返回一个 handle,同时暴露 `session`、`runner`(AgentChatRunner)与 L2 `Agent` 视图;TUI/CLI/测试各自按需订阅。
- provider 由调用方(如 factory)选好传入;模型选择逻辑**不**进 core。
- 持久化(JsonlSessionStore + AgentSession)、内置工具、权限协调、上下文压缩都在这里装配,但 core 自身不 import `lanscoder.app`。

## 4. 消息与事件模型(D2 / D3)

### 4.1 `LoopMessage`(D2,路线 B)

- core 自建轻量消息类型,不与 `context/models.py::AgentMessage` 冲突。
- 命名: 主选 `LoopMessage`,备选 `CoreMessage`(见 §9 开放决策)。
- 桥接: `convert_to_llm(message: LoopMessage) -> ChatMessage`,单向映射到 provider 消息;provider 返回的增量经 `StreamFn` 回灌事件。
- `LoopContext.system_prompt` 复用现有 prompt 装配;不复制 `SessionView`。

### 4.2 10 种 `AgentEvent`(D3)

```python
AgentEvent = (
    AgentStartEvent | AgentEndEvent
    | TurnStartEvent | TurnEndEvent
    | MessageStartEvent | MessageUpdateEvent | MessageEndEvent
    | ToolExecutionStartEvent | ToolExecutionUpdateEvent | ToolExecutionEndEvent
)
```

- `message_update.assistant_message_event` 复用 provider 的 `ChatStreamEvent`。
- L3 的会话事件词汇与 L1/L2 不同(pi 原生设计: `AgentEvent` vs `AgentSessionEvent`);L3 事件不在本 spec 内逐条定义,由 Step 2 实现时对照 pi `packages/coding-agent` 落定。

## 5. `lanscoder/core/` 包结构(目标)

```text
lanscoder/core/
├── __init__.py          # 公共出口: agent_loop / Agent / create_agent_session / LoopMessage / AgentEvent
├── runtime.py           # Step 1: 自 app/runtime.py 原样迁入的装配根 + 模块级辅助函数
├── agent_loop.py        # Step 2: L1(事件流)
├── agent.py             # Step 2: L2
├── session.py           # Step 2: L3 create_agent_session
├── messages.py          # Step 2: LoopMessage / LoopContext / convert_to_llm
└── events.py            # Step 2: 10 种 AgentEvent
```

## 6. 分步改动清单与验收(D5,三步走)

### Step 1 — 装配根搬迁(零行为变化)

- `lanscoder/core/runtime.py` = 现在 `lanscoder/app/runtime.py` 的内容原样搬入(`register_loop_tools` / `create_agent_loop` / `CurrentSessionState` / `AgentChatRunner` 及模块级辅助函数),import 路径不动。
- `lanscoder/app/runtime.py` 变为 re-export shim:`from lanscoder.core.runtime import *` + 显式 `__all__`,保证 `from lanscoder.app.runtime import AgentChatRunner` 等既有 import 零变化。
- 测试约束纳入 core:
  - `tests/test_dependency_directions.py`: 按现有 AST 扫描模式新增 `core` 约束(`core` 不 import `app`;`agent` 不 import `core`)。
  - `tests/test_layer_boundaries.py`: 新增 fresh-interpreter 泄漏检查(`import lanscoder.core` 不泄漏 `lanscoder.app`;`import lanscoder.agent` 不泄漏 `lanscoder.core`)。
- 验收: SC-1 + SC-4(全量测试绿,零行为变化)。
- 风险: shim 漂移 → 用"shim 的 `__all__` 与 core 公开名一致"断言测试兜底。

### Step 2 — 新增 `create_agent_session` + L1/L2 公共 API(只加不改)

- 新增 §5 中 `agent_loop.py` / `agent.py` / `session.py` / `messages.py` / `events.py`,实现 §3/§4 签名与模型。
- L1 复用 Step 1 迁入的装配产物,但公共面不暴露 session(见 §9 开放决策 O1)。
- `create_agent_session` 成为 headless 唯一装配源;TUI 路径暂不切换。
- 新增测试: L1 事件序列顺序、L2 steer/follow_up/abort 语义、`convert_to_llm` 往返、headless smoke(SC-5)、依赖方向(SC-4)。
- 验收: SC-2 + SC-4 + SC-5。

### Step 3 — `app/factory.py` 消费 core(行为保持)

- `create_lanscoder_app` 改为调用 `create_agent_session` 获取 handle,再在 handle 之上挂 TUI 专属部分(命令处理器、订阅 handler)。
- 模型选择逻辑(`ModelStateStore`、model catalog、`_initial_model_profile`)保留在 factory,把选好的 provider 传入 core。
- 验收: SC-3 + SC-4;factory 现有测试回归绿。

## 7. 测试策略

- 每个 Step 独立 PR,先写测试(TDD,符合 D4)再实现。
- 关键门禁: `pytest` 全量、`ruff check lanscoder tests`。
- 边界测试用 fresh-interpreter 泄漏检查,防止 `if TYPE_CHECKING` 与惰性 import 漏网。

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| shim 漂移(Step 1 后 `app/runtime.py` 与 core 不一致) | shim 公开名与 core 对齐断言测试;shim 只 re-export |
| `core.agent_loop` 与 `agent.AgentLoop` 命名混淆 | 文档写清边界;`core` 内统一 `agent_loop`/`Agent` 命名,不引入 `AgentLoop` |
| factory 重构破坏 TUI 行为(Step 3) | Step 3 只改装配来源,不改行为;靠 factory 现有测试回归 |
| L1 session-free 边界成本高 | §9 O1 提供备选路径,评审拍板 |
| core 被 app 反向污染 | 依赖方向 + 层边界测试把 core 纳入约束 |

## 9. 开放决策(评审点)

- **O1 L1 的 session-free 边界如何落地**:
  - 方案 A(推荐): L1 定义全新最小上下文面(`LoopContext` 显式注入),内部适配现有引擎;成本高但 API 干净,完全对齐 pi。
  - 方案 B(渐进): Step 2 先让 L1 以"事件流外推"形态落地(接收已装配 loop,不暴露 session 类型),session 依赖在后续迭代消除。
- **O2 消息命名**: `LoopMessage`(主选)vs `CoreMessage`(备选)。D2 已排除 `AgentMessage`。
- **O3 `create_agent_session` 返回形态**: `AgentSessionHandle(session, runner, agent)` 聚合对象 vs 只返回 `AgentSession`(runner/agent 从 session 派生)。主选聚合对象,便于 TUI 只订阅。
- **O4 L1/L2 是否在 Step 2 一起落地**: 主选一起落地(三层同 PR,验收连贯);若 O1 选方案 A 成本过大,可拆成 Step 2a(L3 + 事件化 L1)/ Step 2b(L2)。

## 10. 验收清单(对应 TASK.md SC-1..SC-5)

- [ ] SC-1 Step 1 零行为变化,全量测试绿
- [ ] SC-2 Step 2 只加不改,新测试绿
- [ ] SC-3 Step 3 factory 消费 core,行为保持
- [ ] SC-4 core/agent 依赖约束纳入自动化测试并绿
- [ ] SC-5 headless smoke:只 import `lanscoder.core`,三层可创建与驱动
