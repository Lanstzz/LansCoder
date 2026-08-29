# SDK 契约与版本(`lanscoder.core`)

> TASK-002(P1)把 `lanscoder.core` 钉死为稳定、可对外发布的 SDK 面:契约测试锁定公开面,
> `py.typed` 开启类型支持,`__version__` 跟随包版本,文档与示例承诺 headless 可用。

## 1. 定位与约束

- `lanscoder.core` 是唯一装配源与 SDK 入口;**不 import `lanscoder.app`、不依赖 TUI**(泄漏检查见
  `tests/test_layer_boundaries.py::test_core_import_does_not_pull_app` 与
  `test_core_import_does_not_pull_tui`)。
- 依赖方向终态:`providers ← context ← agent ← core ← app ← cli/tests`;`lanscoder.agent` 不 import core。
- 公开面 = `lanscoder.core.__all__`(20 个名字),由 `tests/test_core_contract.py` 精确钉死:任何增删都必须
  同步改契约测试,防止无意的 API 漂移。

## 2. 安装与分发包

`lanscoder.core` 以独立分发包 **`lanscoder-core`** 发布(dist 名 ≠ import 名，import 保持 `lanscoder`)。

### 2.1 SDK 安装(headless，无 TUI)

```sh
pip install lanscoder-core          # 必装依赖:anyio / portalocker / PyYAML
pip install "lanscoder-core[llm]"   # + openai / anthropic(真实模型适配器)
pip install "lanscoder-core[mcp]"   # + mcp(外部 MCP 工具服务器)
```

- 安装后即可 `from lanscoder.core import create_agent_session`(L1/L2/L3 全可用)，不安装、不 import Textual。
- 真实模型与 MCP 均为惰性接入:只跑 headless 回合(duck-typed `LlmTransport`)时 `[llm]`/`[mcp]` 都不需要。
- 最小依赖集由 `publish-pypi.yml` 的 `minimal-core-deps` job 在发布期锁定(契约 + SDK 示例 + 层边界泄漏 +
  安装包冒烟)；对应验收 SC-1 / SC-2。

### 2.2 `LansCoder` 与 `lanscoder-core` 的关系(薄壳，非替代)

- `LansCoder`(PyPI 上的 TUI 应用)是**薄壳**:wheel 不再自带 `lanscoder/` 源码树，
  `dependencies = ["lanscoder-core[llm,mcp]", ...]` + TUI 侧依赖(textual / prompt_toolkit / tomlkit / python-dotenv)。
- 二者是**依赖关系**:`LansCoder` 依赖 `lanscoder-core`，**不是**"二选一"的替代关系；`lanscoder/` 导入树由
  `lanscoder-core` 唯一持有，两个 wheel 文件零重叠(D7 / SC-7)。
- 选择:只做二次开发/集成 → 装 `lanscoder-core`；要完整 TUI 应用 → 装 `LansCoder`(自动带上 core)。

### 2.3 版本

- 单一事实来源 `lanscoder/core/_version.py`；root 薄壳版本与 `lanscoder-core==` pin 硬编码一致，
  由 `tests/test_dist_metadata.py`(本地)与发布 tag 校验(CI)强制(D3 / D7a)；一个 tag 同时发布两个 dist。

## 3. API 版本策略

- `lanscoder.core.__version__` 暴露包版本(单一事实来源 `lanscoder/core/_version.py`),契约测试断言其与
  `pyproject.toml` 的 `[project].version` 一致。
- 语义化版本:`lanscoder.core` 是稳定 SDK,破坏性变更(删名字、改签名、改字段)必须提升 major;新增只增
  不改的行为走 minor;bug 修复走 patch。
- 破坏性变更需要:同步更新契约测试 + 本文件 + `examples/sdk/`,并在 CHANGELOG 记录。
- 类型支持:`lanscoder/py.typed`(PEP 561)标记包为内联类型,类型检查器可直接消费 `lanscoder.core` 的注解。

## 4. 三层 API

### 3.1 L1 `agent_loop`(无状态裸循环)

```python
async def agent_loop(
    prompts: list[LoopMessage],
    context: LoopContext,
    config: LoopConfig,
    signal: CancellationToken | None = None,
) -> AsyncIterator[AgentEvent]:
```

- 不落盘(`InMemorySessionStore`)、不承担权限/守卫;事件序列:`agent_start → (turn_start → message_start
  → message_end → turn_end)* → agent_end`。
- 流式三态:`LoopConfig.use_streaming` = `None`(按 `capabilities.supports_streaming` 自动探测)/ `True`
  (强制流式,有 `MessageUpdateEvent`)/ `False`(强制非流式)。

### 3.2 L2 `Agent`(有状态 wrapper)

```python
agent = Agent(context=LoopContext(...), config=LoopConfig(provider=transport, ...))
unsubscribe = agent.subscribe(listener)   # 订阅 AgentEvent
await agent.prompt("hello")              # 驱动一轮
agent.steer(msg) / agent.follow_up(msg)  # 回合后注入
agent.abort()                            # 中断当前回合
```

### 3.3 L3 `create_agent_session`(headless 完整装配)

```python
handle = create_agent_session(
    provider=provider,          # ChatProvider(LlmTransport 结构满足)
    project_root=root,          # 必需
    data_root=root / ".lanscoder",  # 持久化位置(默认 project_root/.lanscoder)
    tools=[custom_tool],        # 自定义工具;None = 内置工具集
    session_id="s1",
    resume=True,                # 恢复既有会话(与 data_root + session_id 配套)
)
handle.session                       # AgentSession
handle.runner                        # AgentChatRunner
await handle.runner.arun_user_turn("hello")
handle.runner.current_session.set_permission_mode("bypass")  # 权限模式
handle.runner.tool_event_handler = audit                       # 工具执行审计
```

- `AgentSessionHandle` 只有 `session + runner`(D2);L2 `Agent` 独立可用,不经 L3 handle。
- 权限模式:`PermissionMode`(`standard` / `aggressive` / `bypass`)或等价字符串。
- `tool_event_handler` 接收 `lanscoder.agent.loop.ToolExecutionEvent`(`kind ∈ started/finished/...`)。
- 恢复:同一 `data_root` + 同一 `session_id` + `resume=True` 重开一个 handle,历史消息在。

## 5. 传输协议 `LlmTransport`(D3)

```python
@runtime_checkable
class LlmTransport(Protocol):
    name: str
    model: str
    capabilities: ProviderCapabilities
    def complete(self, request: ChatRequest) -> ChatResponse: ...
    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]: ...
```

- 复用 `lanscoder.providers.types` 叶子类型;`ChatProvider` 结构性满足(自带 provider 零适配)。
- 外部框架自建模型层:实现 2 方法 + 3 属性即可,无需继承 providers ABC(见
  `examples/sdk/minimal_llm_transport.py`)。

## 6. 可运行示例与门禁

- 示例:`examples/sdk/`(README 见 `examples/sdk/README.md`);`tests/test_sdk_examples.py` 以子进程冒烟
  测试锁定"无 TUI 可运行"(SC-7)。
- 门禁(SC-8):`pytest` 全量 + `node .ai-team/check.mjs --base origin/main` + `ruff check lanscoder tests`。
- 契约测试:`tests/test_core_contract.py`(`__all__` / 签名 / 字段 / `__version__` / `py.typed`)。
