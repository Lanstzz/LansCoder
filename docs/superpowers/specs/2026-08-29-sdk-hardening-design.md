# SDK 硬化设计(spec)

- 日期: 2026-08-29
- 状态: 待评审(Task 0 / TASK-002)
- 上游决策: `2026-08-27-core-three-layer-decoupling-design.md`(TASK-001,已合入)+ 2026-08-28/29 SDK 讨论拍板的 D1-D8(见 §2)
- 关联任务: `.ai-team/TASK.md` TASK-002

## 1. 背景与目标

TASK-001 已完成三层解耦:`lanscoder/core` 承载 L1 `agent_loop` / L2 `Agent` / L3 `create_agent_session`,headless 可用、不拉 TUI。但作为对外 SDK 仍有三处硬伤:

1. **L1 不是真 session-free**:`agent_loop` 每次调用在 temp 目录建 `JsonlSessionStore` 并写临时文件,拖入整个持久化机制(架构不干净,行为上纯属浪费)。
2. **L3 handle 误导**:`AgentSessionHandle.agent` 开着一次性临时会话、不写 L3 的持久化 store,生产代码零消费者,只让使用者误以为"handle 上的 agent 就是本会话的 agent"。
3. **传输契约太厚**:`LoopConfig.provider: ChatProvider` 是具体 ABC,外部框架要么继承它、要么实现全套抽象方法;且 `lanscoder.core` 无 `py.typed`、无契约测试、无 SDK 文档/示例、无 API 版本策略。

目标(P0-P3):让 `lanscoder.core` 成为稳定、可对外发布的 SDK 面——L1 真 session-free、传输窄协议化、契约与文档钉死。P4(独立分发包)不做(D8)。

## 2. 决策(2026-08-28/29 拍板,评审即生效)

| # | 决策 |
|---|------|
| D1 | P2 选方案 A:复用 `agent/` 引擎 + 内存会话,不重写裸循环(推翻原方案 B:不造第二个循环引擎,杜绝行为漂移) |
| D2 | `AgentSessionHandle` 去掉 `agent`,只留 `session + runner`;L2 `Agent` 保持独立(session-free) |
| D3 | P3 保守版 `LlmTransport` Protocol(复用 `providers.types` 类型),`LoopConfig.provider` 字段名不变、类型改为它;不引入 pi 式 `stream_fn` 主 API |
| D4 | L1 流式:自动探测(`capabilities.supports_streaming`)+ 可选 `use_streaming: bool \| None` 覆盖 |
| D5 | L1 行为边界:保留工具多轮往返(复用 `ToolExecutor`);不承担权限/守卫/compaction;保留循环级 `limits/context_window/request_options` |
| D6 | P0 立即提交(用户已提交 `3fab751`,待合入 main) |
| D7 | P1 内容:`py.typed` + 契约测试 + SDK 文档/headless 示例(按 L3 + 每任务短会话驱动形态写)+ API 版本策略;顺序 P0→P2→P3→P1 |
| D8 | P4 独立分发包不做;`lanscoder.core` 文档化为 SDK 入口 |

## 3. 目标 API(P2/P3 落地后)

### 3.1 L1 `agent_loop`(签名不变,内部改 P2)

```python
async def agent_loop(
    prompts: list[LoopMessage],
    context: LoopContext,
    config: LoopConfig,
    signal: CancellationToken | None = None,
) -> AsyncIterator[AgentEvent]:
```

- 内部:P2 用 `InMemorySessionStore` 替代 temp 目录,不落盘、无临时文件;事件序列不变。
- 工具多轮往返保留,`permission_manager=None`(D5)。

### 3.2 `LoopConfig`(P2/P3 后)

```python
@dataclass(slots=True)
class LoopConfig:
    provider: LlmTransport                # D3: 类型改为 core 自有 Protocol
    use_streaming: bool | None = None     # D4: None=自动探测;True/False=强制
    session_id: str = ""
    request_options: MainRequestOptions | None = None
    context_window: int | None = None
    limits: AgentLoopLimits | None = None
    background_manager: BackgroundJobManager | None = None
    guidance_provider: Callable[[], list[str]] | None = None
```

### 3.3 `LlmTransport`(新增 `lanscoder/core/transport.py`)

```python
class LlmTransport(Protocol):
    name: str
    model: str
    capabilities: ProviderCapabilities

    def complete(self, request: ChatRequest) -> ChatResponse: ...
    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]: ...
```

- `ChatProvider`(含默认 `astream`)结构性满足 → 自带 provider 零适配。
- 外部框架自带模型层:实现 2 方法 + 3 属性即可,无需继承 `ChatProvider` ABC。
- 类型复用 `lanscoder.providers.types`(叶子类型,无 app 依赖)。

### 3.4 `AgentSessionHandle`(P3 / D2)

```python
@dataclass(slots=True)
class AgentSessionHandle:
    session: AgentSession
    runner: AgentChatRunner
```

### 3.5 L2 `Agent`(不变)

`subscribe / prompt / steer / follow_up / abort`,内部驱动 L1;不承载持久化。

## 4. 分步改动清单与验收(D7 顺序)

### Step 1(P2)— L1 内存会话 + 流式三态

改动:
- `lanscoder/context/store.py`:新增 `InMemorySessionStore(JsonlSessionStore)`,覆盖全部落盘方法为内存 dict 实现(`append_event` / `list_events` / `rebuild_session_view` / `original_user_message_texts` / `truncate_before_message` / `delete_session`),不建目录、不写盘;复用基类 `_apply_event` 重建逻辑;约 50 行,**零引擎改动**(类型提示 `JsonlSessionStore` 天然兼容子类)。
- `lanscoder/core/agent_loop.py`:temp 目录 → `InMemorySessionStore()`;`use_streaming` 三态决定是否给引擎挂 `stream_event_handler`。
- `lanscoder/core/messages.py`:`LoopConfig` 增加 `use_streaming: bool | None = None` 字段(加字段,不破坏既有调用)。

验收:
- SC-1 L1 不落盘:新增测试断言 `agent_loop` 运行后无临时目录/文件残留(monkeypatch `TemporaryDirectory` 使其失败,或断言 store 为 `InMemorySessionStore`);既有 L1 测试回归绿。
- SC-2 流式三态:None = 有 `capabilities.supports_streaming` 则流式(出现 `MessageUpdateEvent`),True = 强制流式,False = 强制非流式(只有 `MessageEndEvent`);参数化测试。
- SC-3 工具多轮往返与无权限路径不回归:既有 core L1 工具事件测试绿。

### Step 2(P3 + D2)— 传输协议化 + handle 瘦身

改动:
- 新增 `lanscoder/core/transport.py`:`LlmTransport` Protocol;`lanscoder/core/__init__.py` 导出。
- `lanscoder/core/messages.py`:`LoopConfig.provider` 类型 `ChatProvider` → `LlmTransport`(字段名不变)。
- `lanscoder/core/session.py`:`AgentSessionHandle` 去掉 `agent`;`create_agent_session` 不再构造 L2 `Agent`。
- 测试更新:`tests/test_core_session.py` 去掉 `handle.agent` 断言;新增 duck-typed transport 驱动测试。

验收:
- SC-4 外部 duck-typed transport 可驱动 L1:新增测试用**非 `ChatProvider` 子类**的裸对象(2 方法 + 3 属性)跑通 `agent_loop`;并断言 `ChatProvider` 结构性满足 `LlmTransport`。
- SC-5 handle 去 agent 后全量回归绿;L2 `Agent` 独立可用测试保留。

### Step 3(P1)— 契约固化 + SDK 文档

改动:
- 新增 `lanscoder/py.typed`(包根 marker)。
- 新增 `tests/test_core_contract.py`:`core.__all__` 与关键签名钉死(`inspect.signature` 快照);fresh-interpreter 泄漏检查并入既有 `test_layer_boundaries.py`。
- 新增 SDK 文档 `docs/architecture/03-sdk.md` + 可运行示例 `examples/sdk/`(按 D7 形态:`create_agent_session + 自定义知识工具 + set_permission_mode + tool_event_handler 审计 + resume`;另含一个最小 `LlmTransport` 接入示例)。
- API 版本策略:`lanscoder/core/__init__.py` 暴露 `__version__`(跟随包版本),文档声明 `lanscoder.core` 为稳定 SDK 入口、语义化版本。

验收:
- SC-6 `py.typed` 存在且契约测试绿;泄漏检查仍绿。
- SC-7 示例在无 TUI 环境可运行(headless smoke)。
- SC-8 全量 `pytest` / `node .ai-team/check.mjs --base origin/main` / `ruff check lanscoder tests` 绿。

## 5. 测试策略

- 每 Step 独立 PR,先写测试(TDD,D4)再实现。
- 关键门禁: `pytest` 全量、`node .ai-team/check.mjs --base origin/main`、`ruff check lanscoder tests`。
- 泄漏检查延续既有 fresh-interpreter 模式。

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| `InMemorySessionStore` 与 `JsonlSessionStore` 行为漂移 | 继承 + 覆盖全部落盘方法;内存实现复用基类 `_apply_event` 重建逻辑;L1 既有测试回归 |
| `LlmTransport` 类型收紧破坏既有 provider 调用 | `ChatProvider` 结构性满足(含默认 `astream`);字段名不变;全量回归 |
| handle 去 `agent` 属破坏性 API 变更 | 无生产消费者(仅测试);SDK 未发布稳定版,趁早改 |
| P1 契约测试把未定稿签名钉死 | 顺序 P2→P3→P1,契约测试在最终形态之后写 |
| P0 尚未合入 main | P0 独立 PR(`3fab751`);本 spec/TASK 不依赖 P0 代码,可并行 |

## 7. 验收清单(对应 TASK.md SC-1..SC-8)

- [ ] SC-1 L1 不落盘(内存会话)
- [ ] SC-2 L1 流式三态(`use_streaming` 自动/强制/关闭)
- [ ] SC-3 L1 工具多轮往返与无权限路径回归绿
- [ ] SC-4 `LlmTransport` Protocol:duck-typed transport 驱动 L1,`ChatProvider` 结构满足
- [ ] SC-5 `AgentSessionHandle` 去 `agent` 后全量回归绿
- [ ] SC-6 `py.typed` + 契约测试绿,泄漏检查绿
- [ ] SC-7 SDK 文档 + headless 示例可运行
- [ ] SC-8 全量 pytest / check.mjs / ruff 绿
