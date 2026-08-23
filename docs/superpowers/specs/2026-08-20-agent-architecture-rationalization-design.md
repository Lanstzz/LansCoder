# Agent 架构合理化重构 — 设计说明

日期：2026-08-20
基线：HEAD c7ce281（工作树干净）
状态：待用户审阅

## 1. 背景与问题

agent 层（`lanscoder/agent/`，共 14 文件、约 4600 行）当前有六个结构性问题：

### 1.1 God class：`AgentLoop`（loop.py，1330 行 / 67 方法）

`AgentLoop` 是唯一的真 god class。问题不在体量，在**混杂**——至少 7 类互不相同的逻辑堆在一个对象里：

| 簇 | 代表方法 | 性质 |
|---|---|---|
| 轮次编排 | run/nudge/resume、`_run_tool_loop`、`_begin_turn`/`_complete_turn` | 编排（该留） |
| provider 请求构造 | `_prepare_main_provider_request`、`_build_provider_messages`、`_provider_tool_definitions`、`_augment_tool_definition`、`_main_chat_request`、`_request_messages`、`_context_budget_for_view` | 纯变换（`_build_provider_messages`/`_request_messages`/`_main_chat_request`/`_context_budget_for_view` 可移；`_provider_tool_definitions`/`_augment_tool_definition` 依赖每轮 MCP 状态留 loop，见缝 1） |
| provider 交互 + 恢复 | `_complete_once`、`_complete_once_with_recovery`、`_report_progress` | 编排（该留） |
| 上下文压缩决策 | `_compact_if_needed`、`_compact_for_prompt_too_long` | 策略（两条压缩路径都留在 loop，依赖每轮状态，见缝 1） |
| 权限恢复子流程 | `_append_permission_resume_result` → `_finish_permission_resume` 一串 | 子流程（可移） |
| 护栏/限额 | `_check_provider_call_limit`、`_reserve_provider_call`、`_check_turn_timeout`、`_tool_round_limit_response`、`_limit_response`、`_interrupted_response` | 策略（可移） |
| 旁路集成 | delegate 注册、background 注入、MCP 校验 | 接线（可移部分） |

### 1.2 跨模块 import 环：`loop ↔ subagent`

- `loop.py:67` 模块级 `from lanscoder.agent.subagent import SubagentRunner`——唯一原因是 `_ensure_delegate_tool` 要构造具体 runner
- `subagent.py:176/279` 方法内惰性 `from lanscoder.agent.loop import AgentLoop`——子代理要构造 child loop

结果：模块级 import 图含环，靠 subagent 侧惰性 import 兜着。这是"加载顺序地雷"——任何人在 subagent.py 顶层加一条 `from lanscoder.agent.loop import X` 就变成导入时硬崩溃。

注意：`tools → agent` 分层违规已在 `bd74ea0`（新增叶子模块 `lanscoder/subagent/types.py`）+ `7bbeb45` 修复，`loop ↔ subagent` 是剩余的同层环。

### 1.3 权限 split-brain：一个领域归三个模块管

- `session.py`：`set_permission_mode`/`_sync_sandbox_access_with_mode` 一次改动同步四份状态（mode + permission_manager + sandbox_access + policy dict）
- `tool_execution.py`：`_prepare_permission`/`_prepare_bypass_mutation`/pending 存储，反向 `self.session.preflight_tool_call_permission(...)` 穿透 session
- `subagent.py`：各自 ad hoc 构造 PermissionManager
- `loop.py`：权限恢复子流程协调三者

### 1.4 观测通道碎片化

loop 上有 `progress_callback` / `tool_event_handler` / `stream_event_handler` 三条回调 + `foreground_subagent()` / `usage_summary()` 两个查询，五个口子互不相干。

### 1.5 装配责任在类内

`AgentLoop.__init__` 自己注册调用方工具、注册 background 控制工具、注册 delegate 工具、构造 ToolExecutor、构造 SubagentRunner。类既是运行器又是装配器。

### 1.6 subagent 模块乱象（第二处 god class + 命名撞车）

"subagent" 一个概念散在 4 个文件、跨 4 层：`lanscoder/subagent/`（叶子领域包）、`lanscoder/agent/subagent.py`（具体 runner）、`lanscoder/tools/delegate.py`（工具适配器）、`lanscoder/app/subagent_panel_state.py`（TUI）。具体问题：

- **第二个 god class**：`SubagentRunner`（691 行 / 19 方法）混着 run 分派、child 会话生命周期、worktree 管理、3 条手搓 PermissionManager 路径、prompt 组装、进度追踪、child 工具装配。
- **自带双树**：`_run_inline` 与 `_run_isolated` 近乎重复"构造 AgentLoop（9 kwargs）→ asyncio.run → 收割 SubagentResult"的尾巴（isolated 多装饰 diff/worktree 字段）——与 loop.py 已消灭的双树是同一个 smell。
- **命名撞车**：`SubagentRunner` 同时是叶子 Protocol（`tools/delegate.py` import）和 agent 层具体类（`loop.py` import）；`lanscoder.subagent`（包）与 `lanscoder.agent.subagent`（模块）同名两处。
- **可接受的不变式**：`runner.run()` 同步阻塞（内部 `asyncio.run()`），靠工具执行在 worker 线程成立。这是设计决策，不改成 async，用契约测试钉住。

## 2. 目标

1. `AgentLoop` 从 67 方法降到约 30，剩下的全是编排（状态迁移 + 接线）。
2. 模块级 import 图**无环**，单一组装根在 `app/` 层完成所有跨模块装配。
3. 权限领域收敛到单一协调器，一处归管。
4. 观测收敛为单一 `TurnObserver` 契约。
5. 工具注册、runner 装配全部上移组装根。
6. subagent 模块内部合理化：去双树、child 权限归口、命名消歧（缝 5b/5c/5d）。
7. **每个缝都有双向契约测试，任一方违约立即爆红**（用户明确要求）。

## 3. 范围决策（已与用户确认）

- **重构范围**：7 刀全做（见 §5），其中缝 5 扩为 subagent 合理化（5a 断环 / 5b 去双树 / 5c 权限归口 / 5d 命名消歧）。
- **组装深度**：完整注入。`tool_executor`/`observer`/`request_builder`/`guardrails`/`permission_resume`/`subagent_runner` 全部由组装根构造注入，`AgentLoop` 变纯运行器。
- **公共 API**：允许重塑，但必须配套契约测试——缝的两侧（消费者与提供者）任一方违约即爆红。
- **subagent 保留不变**：叶子包 `lanscoder/subagent/` 的顶层放置（拆环手法）不重命名；`runner.run()` 同步阻塞是接受的不变式，不改成 async；`app/subagent_panel_state.py` 不动。
- 本次 session 交付：本 spec + 实现计划文档。实现走 spec → plan → SDD 流程，另起。

## 4. 目标架构

### 4.1 依赖图（目标：模块级无环）

```
app/（组装根：runtime.py 完成全部跨模块连线）
  │
  ├──▶ agent.loop.AgentLoop            （纯运行器，只收引用）
  ├──▶ agent.subagent_engine.SubagentEngine（接收 child 工厂）
  ├──▶ agent.tool_execution.ToolExecutor
  └──▶ ...（observer / permission / request_builder 等）

agent/ 内部单向：
  loop.py ──▶ request_builder, guardrails, permission_resume, session,
              tool_execution, ports(SessionTurnRunner), subagent/types(Protocol)
  subagent_engine.py ──▶ ports(SessionTurnRunner), session, background, worktree, permissions
  tool_execution.py ──▶ permission(Coordinator), session
  session.py ──▶ context/store, permissions, skills, tools

subagent/types.py = 叶子（无 lanscoder 依赖，已就位）
```

具体类与 Protocol 的实现/消费关系全部经组装根解析，模块级不再互相 import。

### 4.2 模块地图

| 文件 | 职责 | 状态 |
|---|---|---|
| `agent/loop.py` | `AgentLoop` 纯编排 | 重构 |
| `agent/request_builder.py` | 请求构造（纯） | 新增 |
| `agent/guardrails.py` | 限额/超时策略 + 限流响应 | 新增 |
| `agent/permission_resume.py` | 权限恢复子流程 | 新增 |
| `agent/permission.py` | 权限领域协调器（mode/policy/sandbox/preflight/pending） | 新增 |
| `agent/observer.py` | `TurnObserver` 观测契约 | 新增 |
| `agent/mcp_activation.py` | `McpActivationTracker`（MCP 激活状态，解 ToolExecutor 构造环） | 新增 |
| `agent/ports.py` | `SessionTurnRunner` 等协议 | 扩展 |
| `agent/session.py` | 会话状态/持久化/registry 门面 | 减负（权限部分移出） |
| `agent/tool_execution.py` | 工具执行引擎 | 减负（权限部分移出，事件走 observer） |
| `agent/subagent_engine.py`（原名 `subagent.py`，缝 5d 改名） | 子代理运行器 `SubagentEngine` | 构造签名改（接收 child 工厂） |
| `app/runtime.py` | 组装根 | 增负（装配） |

## 5. 七个缝

每个缝的拆法、落点、契约、以及**什么必须留在 loop**。

### 缝 1 — RequestBuilder（`agent/request_builder.py`，纯）

**从 loop 移出**（纯变换，输入全部显式传入，无任何 loop 字段依赖）：
- `_build_provider_messages`、`_request_messages`（不再内嵌 `rebuild_view`，view 由调用方传入）、`_main_chat_request`、`_context_budget_for_view`。

**留在 loop**：
- `_prepare_main_provider_request` 的壳——前置步骤 `_repair_interrupted_tool_calls_before_provider_request`、`_check_cancelled`、`_append_pending_guidance`、`_append_background_notifications` + `rebuild_view` + 调 `request_builder.build`
- `_compact_if_needed` / `_compact_for_prompt_too_long`（决策 + 调 context_manager + 成功后 `rebuild_view`——AUTO 与 PROMPT_TOO_LONG 两条压缩路径都留 loop，避免同一关切被劈成两半）
- `_provider_tool_definitions` / `_augment_tool_definition`（依赖每轮 MCP 激活状态，经注入的 `McpActivationTracker.active_names`，见缝 6）
- `_record_projection_consumed`（写 session 记录）

**公共访问器**：`context_budget_for_view`（runtime.py 依赖）移入 RequestBuilder——公共 API 允许重塑，契约测试钉新位置。`AgentChatRunner.context_budget`（runtime.py:376-378）改为 `self.request_builder.context_budget_for_view(view, runtime_instruction=None, definitions=self.loop._loop_tool_definitions())`——`self.loop` 是 AgentChatRunner 持有的当前 loop，`_loop_tool_definitions()` 是 loop 保留的 `_provider_tool_definitions` 的公共薄访问器（组装根把 loop 传给 runtime）。不写清 definitions 来源，runtime 会自己再造一遍 augment 逻辑。`context_budget` 保留空状态 fallback（`/context` 命令可能在任何 turn 之前触发，factory.py:332 把它当 budget_provider）：`self.loop` 为 None 时，`definitions` 退化为从 registry 读 `_provider_tool_definitions` 等价物（经注入的 `McpActivationTracker` 过滤），**不再构造即弃 loop**；`self.loops` 列表仅作调试历史保留，不再被 `context_budget` 读取。

**接口**（`definitions` 为显式入参）：
```python
class RequestBuilder:
    def build(self, view, *, definitions, tool_choice="auto", runtime_instruction=None) -> PreparedMainRequest
    def context_budget_for_view(self, view, *, runtime_instruction, definitions) -> ContextBudget
```
`build` 是纯函数：view + definitions + config → `PreparedMainRequest`（含 request、request_id、fingerprint、tool_result_part_ids）。零 loop 状态，独立可测。**构造依赖**：`RequestBuilder.__init__` 需注入 `session`、`context_builder`、`request_options`、`context_window`（均来自 runtime/组装根，与缝 5a 的装配清单同风格）。

### 缝 2 — TurnGuardrails（`agent/guardrails.py`，策略）

从 loop 移出：`_check_provider_call_limit`、`_reserve_provider_call`、`_check_turn_timeout`、`_tool_round_limit_response`、`_limit_response`、`_interrupted_response`。

**留在 loop**：`_is_cancelled`/`_check_cancelled`（注入 token 的薄包装，属于运行器）。

**状态归属**：`provider_call_count` 与 `turn_started_at` 随方法移入 guardrails 持有——这两个计数器本在 `_begin_turn` 里每轮重置（loop.py:1176），guardrails 必须提供 `begin_turn()`，loop 的 `_begin_turn` 改为调用 `self.guardrails.begin_turn()`。漏掉这条，第 2 轮起立即触发 provider 调用上限或超时。**读取方（P2）**：`_report_progress` 从 `self.guardrails` 读 `provider_call_count`（guardrails 提供 `call_count` 只读访问器），`total_tokens` 留 loop 累积；`loop.provider_call_count` 的既有测试断言（如 test_model_request_options.py:189）迁移为经 guardrails 访问。

**配套移动**：`_AgentLoopLimitReached` 异常与 `AgentLoopStopReason` 枚举一并从 loop.py 移入 `loop_limits.py`（数据层；枚举被异常、guardrails、loop 出口三方使用）；`clock`（超时计时）由 guardrails 持有。

**构造依赖**：guardrails 需要 `provider`（`_limit_response`/`_interrupted_response` 构造响应用 `provider.name`/`provider.model`）、`limits`、`clock`。

**接口**：
```python
class TurnGuardrails:
    def begin_turn(self) -> None              # 重置 provider_call_count=0、turn_started_at=clock()
    def reserve_call(self) -> None            # 抛 _AgentLoopLimitReached
    def check_timeout(self) -> None           # 抛 _AgentLoopLimitReached
    def limit_response(self, reason) -> ChatResponse
    def interrupted_response(self) -> ChatResponse
```
与 `loop_limits.py`（数据）配对成"数据 + 策略"。让"限额到了怎么办"成为单一策略对象。

### 缝 3 — PermissionResumeHandler（`agent/permission_resume.py`，子流程）

从 loop 移出：`_append_permission_resume_result` → `_finish_permission_resume` 一串（`_pending_permission_for_resume`、`_prepare_permission_resume`、`_execute_resumed_permission_tool_call`、`_emit_finished_permission_resume`、`_resolve_pending_confirmation`、`_blocked_permission_resume_result`）。

拥有**纯部分**：pending 查找、answer→decision 映射、执行、finish。返回**三值判别式结果**——现有 `_append_permission_resume_result` 返回 `AgentTurnResult | None` 有三种形状，两值判别式会漏掉终止路径：
```python
@dataclass(frozen=True)
class ResumeOutcome:
    kind: Literal["continue", "wait_for_input", "finished"]
    result: AgentTurnResult | None = None
    # kind == "continue"：整批跑完，进入下一轮工具循环
    # kind == "wait_for_input"：链式 pending，携带 WAITING_FOR_USER_INPUT 结果
    # kind == "finished"：请求不存在或无 permission_manager，携带 COMPLETED/error 结果，本轮立即结束
```
**留在 loop**：看到 "continue" 就再进 `_run_tool_loop` 的 glue；看到 "finished" 直接返回其结果。这是整个拆分里接口设计最需要小心的一刀。

**pending 查找依赖**：`_pending_permission_for_resume` 的 pending 查找经一个最小 `PendingStore` 协议（`get(request_id)`/`clear()`）注入 handler；本步实现为 session 的薄适配，步骤 7 缝 4 只换实现不改 handler 逻辑——避免同一段代码被重写两遍。

### 缝 4 — PermissionCoordinator（`agent/permission.py`，领域收敛）

新建协调器，拥有权限领域全套：`permission_mode`、`permission_manager`、`sandbox_access`、`permission_policy`、preflight、pending 存储、review/bypass 决策。

- `session.py`：移出 `set_permission_mode`/`_sync_sandbox_access_with_mode`。session **没有也不应有 `.permissions` 属性**（当前代码无此字段，TUI/runtime 也不读它）——改为：session 仅保留 `permission_mode` 只读访问器（返回协调器当前 mode）供 TUI；session 不再持有 `permission_manager`/`sandbox_access`/`permission_policy` 状态字段
- `tool_execution.py`：`_prepare_permission`/`_prepare_bypass_mutation`/pending 改为直接依赖协调器，不再穿透 session
- `subagent_engine.py`（缝 5c，步骤 5 已改名）：3 条 child 权限路径（inline/isolated/background）改用协调器，按隔离模式产出 child 权限管理器，不再 ad hoc 现造

**接口**（逐方法钉契约与现调用点映射，防协调器退化成 session 现方法的薄透传）：
```python
class PermissionCoordinator:
    def set_mode(self, mode: PermissionMode) -> PermissionMode
    def preflight(self, tool_call) -> ToolPermissionPreflight | None      # ← session.preflight_tool_call_permission（tool_execution.py:408/:481）
    def prepare(self, tool_call, deferred_tool_calls) -> PreparedPermission  # 替换 _prepare_permission 的整体编排，判别式数据类含 preflight+pending+bypass_result，替换 _PermissionPreparation 的活口
    def store_pending_request(self, *, tool_call, request, deferred_tool_calls, review_only) -> UserInputRequest | ToolResult  # ← store_pending_permission_request / store_pending_ask_user（:557/:609）
    def requires_review(self, tool_call) -> bool
    def bypass_mutation(self, tool_call, *, preflight) -> ToolResult | None  # ← _prepare_bypass_mutation（:503）
```
调用顺序：`preflight` →（`requires_review` 时）`store_pending_request` → `bypass_mutation`，写入 §6.3 作为行为不变式。

### 缝 5 — subagent 合理化（组合根 + 去双树 + 权限归口 + 命名消歧）

**5a 断 subagent↔loop 环（组合根）**

- `agent/ports.py` 新增 `SessionTurnRunner` Protocol（子代理需要的最小面，已核实 subagent 只调用 `run_user_turn` + `usage_summary`）：
```python
class SessionTurnRunner(Protocol):
    async def run_user_turn(self, content: str) -> AgentTurnResult: ...
    def usage_summary(self) -> dict[str, int]: ...
```
- `SubagentEngine.__init__` 接收 `child_runner_factory: Callable[..., SessionTurnRunner]`——child AgentLoop 的构造（`enable_delegate_tool=False`、`background_manager=None` 等差异）移出 subagent.py 进组装根。
- `AgentLoop` 接收注入的 `subagent_runner`（叶子 Protocol），不再构造。
- 结果：`loop.py` 模块级不再 import `agent.subagent`；`subagent_engine.py` 模块级不再 import `agent.loop`。模块级 import 图无环。
- **叶子 `SubagentRunner` Protocol 成员显式化**（P2）：`lanscoder/subagent/types.py:129` 的 Protocol 需显式声明 `run` + `foreground_progress`（loop 的注入点与 observer 的 provider 都依赖这两个成员），并加 Protocol 合规契约测试。

**组装根装配清单**：SubagentEngine 构造共 11 参——`store`/`provider`/`request_options`/`limits` 来自 runtime；`tools` 来自 `session.tool_registry.tools()`（缝 7 已保证注册完备）；`project_root`/`agents_md`/`skill_catalog` 来自 runtime 配置；`permission_manager`/`sandbox_access` 来自 PermissionCoordinator（缝 4，步骤 7 前先传 session 现值，5c 归口后换协调器）；`background_manager` 来自 runtime。加 `child_runner_factory` 共 12 个依赖。

**child 工厂签名**（逐轮调用，工具与进度按路径注入，provider/limits/request_options 及 `enable_delegate_tool=False`/`background_manager=None` 由组装根闭合）：
```python
# subagent_engine.py 不再 import AgentLoop，每轮调用：
runner = self.child_runner_factory(
    session=child_session,
    tools=path_tools,          # inline=_tools_for_role(role)；isolated=_worktree_child_tools(...)
    observer=child_observer,   # 由 subagent 引擎构造：on_progress 承载原 _make_progress_callback
                               # 的去向分流（current_job_id() 非 None → 写 BackgroundJob.progress；
                               # 否则写前台 progress_tracker），usage_summary 供 SubagentResult 收割
    cancellation_token=...,
)
```
`_make_progress_callback` 的语义迁入 child observer 的 `on_progress`，`progress_callback` 参数从工厂签名与 loop 构造中一并消失（缝 6 已把它收敛进 TurnObserver，避免工厂向不收 progress_callback 的签名传参）。`cancellation_token` 由 subagent 引擎**每次调用工厂时**取 `current_cancellation_token()`（上下文局部语义，不在组装根捕获；token 是每轮动态值，不是构造期闭包）。工厂被调用的参数集与现 9 kwargs 差异：session/tools/observer/cancellation_token 传入，provider/limits/request_options/`enable_delegate_tool=False`/`background_manager=None` 由组装根闭合。tools 的逐路径计算（`_tools_for_role`/`_worktree_child_tools`）留在引擎，不进工厂。

**5b 去双树**

`_run_inline` / `_run_isolated` 共用的"建 child loop → run → 收割 SubagentResult"尾巴抽成单一 `_run_child_loop(child_session, tools, progress)`；两条路径只差"怎么建会话 / worktree / 工具"（inline = parent-rooted 工具；isolated = worktree 工具 + diff 装饰）。消灭 run-tail 重复。

**5c 权限归口（并入缝 4）**

`_child_permission_manager_for_inline` / `_child_permission_manager` / `_background_child_permission_manager` 三条手搓 PermissionManager 路径收敛进 `PermissionCoordinator`（或 child-session 工厂），按"隔离模式"产出一个 child 权限管理器，不再 ad hoc 现造。

**5d 命名消歧**

`SubagentRunner` 目前同时是叶子 Protocol（`tools/delegate.py` import）与 agent 具体类（`loop.py` import）——同名两类型。同一机械步骤内两处一起改：
- 具体类改名 `SubagentRunner` → `SubagentEngine`，Protocol 保留 `SubagentRunner`；`agent/subagent.py` 内定义类名同步改。
- **模块改名** `lanscoder/agent/subagent.py` → `lanscoder/agent/subagent_engine.py`——`lanscoder.agent.subagent`（模块）与 `lanscoder.subagent`（包）同名两处本身是 §1.6 列的毛病，只改类名撞车照旧。5d 本就要 touch 所有 import 点（loop.py 注入点、组装根、`tests/test_delegate_tool.py` 等），边际成本近零，一次清零。
- 叶子包 `lanscoder/subagent/` 放置与命名不变（拆环手法，§3）。

**公共 API 显式决策（P1）**：三个 sync facade `_run_user_turn_sync`/`_run_nudge_turn_sync`/`_resume_with_user_input_sync`（loop.py:248/293/343）是约 120 处测试的同步入口（96/2/22），**保留为薄 facade**（`asyncio.run(内部 async 核心)`），不改行为、不删。§6.3 加契约测试 `test_sync_facade_contract_signature` 钉三个方法参数名与返回 `AgentTurnResult`。注释注明"删改任一 facade 必须同步迁移全部 120 个测试调用点"。

### 缝 6 — TurnObserver（`agent/observer.py`，观测收敛）

把 `progress_callback` / `tool_event_handler` / `stream_event_handler` / `foreground_subagent` / `usage_summary` 收敛为单一契约：
```python
class TurnObserver:
    def on_turn_started(self, ...) -> None
    def on_progress(self, provider_calls: int, total_tokens: int) -> None
    def on_tool_event(self, event: ToolExecutionEvent) -> None
    def on_stream_event(self, event: ChatStreamEvent) -> None
    def foreground_progress(self) -> dict[str, Any] | None
    def usage_summary(self) -> dict[str, int]
```
注入 loop 与 ToolExecutor（替代 `emit_event=self._emit_tool_event` 回边）。缝 5 的完整注入依赖它——ToolExecutor 移出后必须有独立事件通道。

**foreground 来源与接线时机**：observer 构造注入 `foreground_progress_provider: Callable[[], dict[str, Any] | None]`（组装根用 lambda 引 `subagent_runner.foreground_progress`）。该 provider 的接线依赖缝 5 的 runner 注入，**步骤 4 时还不存在**——步骤 4 只收敛三个原回调通道（progress_callback/tool_event_handler/stream_event_handler）对应的 observer 方法，`foreground_progress` 查询暂留 loop 读 `_delegate_runner`，步骤 5 换成 observer 的 provider。runtime.py:410-413 改为 `loop.foreground_progress()`（loop 保留薄转发），tui.py 的 `getattr(..., lambda: None)` 兼容保持。

**usage_summary 归属**：AgentLoop 保留薄 `usage_summary()` 委托 observer，以维持 `SessionTurnRunner` Protocol 合规（缝 5 的子代理仍经 runner 调 `usage_summary`）。

**注入面收窄（P2）**：ToolExecutor 只注入含 `on_tool_event` 的 `ToolEventSink` 子协议，而非完整 `TurnObserver`（`on_turn_started` 是轮次级事件，与工具执行无关）。

**ToolExecutor 的其余三个构造回调同解（P0，构造顺序环）**：`emit_event` 已由 ToolEventSink 解掉，但 `check_cancelled`/`validate_tool_call`/`observe_tool_result` 目前仍是 loop 方法（loop.py:190/192/193 传给 ToolExecutor）——组装根在 loop 之前构造 ToolExecutor 时这三个回调无法提供，构成构造顺序环。解法：

1. **新增 `agent/mcp_activation.py::McpActivationTracker`**，持有 MCP 激活状态（缝 7 保证 registry 已完备，`_mcp_tool_names` 构造时从 registry 派生）：
   ```python
   class McpActivationTracker:
       def __init__(self, mcp_tool_names: frozenset[str]) -> None: ...
       def clear(self) -> None                          # 原 _begin_turn 的清空逻辑
       def validate(self, tool_call: ToolCall) -> ToolResult | None   # 原 _validate_mcp_tool_call 纯逻辑
       def observe(self, tool_call: ToolCall, result: ToolResult) -> None  # 原 _observe_mcp_search_result 纯逻辑
       @property
       def active_names(self) -> frozenset[str]: ...    # 供 _provider_tool_definitions 读取
   ```
   组装根构造 tracker，注入两侧：loop 的 `_begin_turn` 改为调 `tracker.clear()`，`_provider_tool_definitions` 改读 `tracker.active_names`（方法仍留 loop，**缝 1 的"留在 loop"理由同步改为"依赖每轮 MCP 激活状态（McpActivationTracker）"**）；ToolExecutor 收 `validate_tool_call=tracker.validate`、`observe_tool_result=tracker.observe`。
2. **`check_cancelled` 构造参数去掉**：ToolExecutor 已持有 `cancellation_token`（`replace_cancellation_token` 会同步重绑它，loop.py:236-240），该回调与其自身 token 冗余。ToolExecutor 内部改为对 `self.cancellation_token` 做 None-guard 的 `raise_if_cancelled()`。loop 自身保留 `_check_cancelled`（缝 2 已定留在 loop，供 loop 内部用），不受影响。

**handler 动态槽与 resume 重绑（P1）**：runtime 的 `stream_event_handler`/`tool_event_handler` 字段保留为可写槽（tui_view.py:148-242 每轮 `setattr` 安装/恢复，不改）。`create_agent_loop` **每次 turn 构造新的 observer**，构造时读 runtime 这两个字段当前值写入 observer（等价现状 `_create_loop` 的 runtime.py:399/405）。loop 保留 `stream_event_handler`/`tool_event_handler` 两个**透传 setter**（写入内部 observer），使 runtime `_resume_turn` 的重绑代码（runtime.py:227-237）形状不变；`replace_cancellation_token` 保留并同步重绑注入的 tool_executor 与 observer 的 token 引用。

**loop 保留属性（P2）**：loop 保留公开 `tool_executor` 属性（存放注入实例，约 20 处测试直调 `loop.tool_executor.execute_interactive(...)`）与 `replace_cancellation_token`/`clear_stream_events`（resume 路径）。

### 缝 7 — 工具注册上移（组合根）

调用方 tools、background 控制工具、delegate 工具的注册全部进组装根。`AgentLoop.__init__` 不再注册任何工具，只收引用。

**工具列表单一源（P1）**：`session.tool_registry` 是唯一工具真相。runtime 的 `tools`/`tools_provider`/`_current_tools()`（runtime.py:371-374）保留为 **registry 的喂入源**：每次 turn 构造 loop 前，组装根调 `_current_tools()` 并把结果幂等注册进 registry（与现状 loop.__init__ 的 dedup 注册等价）。`AgentLoop` 构造签名**删除 `tools` 参数**（注册已上移，参数变为死输入）；loop 与 SubagentEngine 统一读 registry；registry 亦为 `McpActivationTracker` 的 `_mcp_tool_names` 派生源（见缝 6）。

**顺序依赖**：缝 5 里 runner 的工具列表依赖"注册完备的 registry"，所以缝 7 必须先于缝 5，让 registry 在 loop 存在前已完备。

## 6. 契约测试策略（一等公民）

**原则**：每个缝的接口都有双向契约测试——消费者侧（按文档签名调用、断言文档形状）与提供者侧（断言实现仍满足签名/协议）——**任一方违约即测试爆红**。契约测试是这次公共 API 允许重塑的护栏。

### 6.1 落点

- `tests/test_agent_contracts.py`（新增）：每个缝的契约测试
- `tests/test_layer_boundaries.py`（扩展）：模块级 import 图无环断言 + 层边界断言

### 6.2 机制

| 机制 | 做法 | 覆盖 |
|---|---|---|
| 签名钉死 | `inspect.signature()` 断言参数名/种类/返回注解 | 每个公共入口 + 每个协作对象接口 |
| Protocol 合规 | 具体类被断言结构性满足 Protocol（`@runtime_checkable`） | SessionTurnRunner、SubagentRunner（叶子）、RequestBuilder 等 |
| 行为不变式 | 用已知输入调用、断言文档化的返回形状与状态迁移 | run_user_turn 的 AgentTurnResult 状态、WAITING_FOR_USER_INPUT、判别式结果 |
| 层边界 | import 图断言：loop 模块级不 import subagent，tools/background 不 import agent | 环回归护栏 |

### 6.3 违约即爆红的具体形态

例：
```python
def test_run_user_turn_contract_signature():
    params = list(inspect.signature(AgentLoop.run_user_turn).parameters)
    assert params == ["self", "content", "attachments", "streaming"]

def test_session_turn_runner_protocol_contract():
    assert isinstance(loop, SessionTurnRunner)          # 结构性协议检查
    assert inspect.signature(loop.run_user_turn).parameters["content"] ...
```
签名改了、方法删了、返回形状变了、模块级边加了——都直接红。

行为级契约测试（接口型缝之外，钉"谁调谁/顺序"类缝）：

- `test_resume_outcome_discriminant`：`ResumeOutcome` 三种 kind 各钉一个用例——整批跑完→continue；链式 pending→wait_for_input（结果 status==WAITING_FOR_USER_INPUT）；request_id 不匹配→finished（结果 status==COMPLETED 且 finish_reason=="error"）。
- `test_permission_coordinator_call_order`：驱动一次工具执行，断言调用顺序为 preflight →（requires_review 时）store_pending_request → bypass_mutation（钉缝 4 行为不变式）。
- `test_registry_complete_before_loop`：用组装根接线构造后，断言 AgentLoop 构造前 registry 已含 caller + background 控制 + delegate 工具（钉死缝 7 顺序依赖），且 `_current_tools()` 的每个工具已在 registry（单一工具源）。
- `test_observer_consumer_pipeline`：驱动一次 `run_user_turn`，断言 on_progress/on_tool_event/on_stream_event 被依次调用且负载形状符合文档（消费者侧，钉缝 6）。
- `test_subagent_engine_no_module_loop_import`：`importlib` 断言 `agent/subagent_engine` 模块级无 `agent.loop`（环回归护栏，补强缝 5）。
- `test_mcp_activation_tracker_turn_boundary`：复用同一 loop 跑两轮（对齐 `test_agent_loop_clears_mcp_activation_on_next_user_turn`），断言第二轮 `_provider_tool_definitions` 不含未激活 mcp 工具——钉死"`_begin_turn` → `tracker.clear()`"每轮接线（P0）。
- `test_sync_facade_contract_signature`：`inspect.signature` 钉 `_run_user_turn_sync`/`_run_nudge_turn_sync`/`_resume_with_user_input_sync` 的参数名与返回 `AgentTurnResult`（P1）。
- `test_resume_rebind_observers`：复用暂停 loop、重绑 handler，断言下一事件进入新 handler（钉缝 6 的 resume 重绑，P1）。
- `test_loop_tool_executor_attribute_contract`：`loop.tool_executor` 是注入的 ToolExecutor 实例且 `execute_interactive` 可调（钉 loop 保留属性，P2）。

## 7. 行为保持（不可破坏的不变式）

以下行为只重构不改变，由现有 1400 测试 + 新契约测试共同守护：

1. turn 语义：`run_user_turn`/`run_nudge_turn`/`resume_with_user_input` 的状态机（COMPLETED / WAITING_FOR_USER_INPUT / 限额 / 中断）
2. tool loop：模型调用工具 → ToolExecutor 执行 → tool_result 落库 → 继续或停止
3. streaming：`astream` 事件收集、partial-delta 回滚、message_completed 缺失即抛
4. 非流式走 worker 线程；compaction 不阻塞事件循环
5. 取消：CancellationToken 注入、`_is_cancelled` post-check、中断工具结果补写
6. 权限：preflight / ask / review-only / bypass / pending 恢复
7. MCP 工具校验：`_validate_mcp_tool_call` 拒绝非活动 mcp 工具（mcp_activation_required）——随缝 1 移动被触及
8. 上下文投影记录：`_record_projection_consumed` 写 request_id/fingerprint/part_ids 到 session——随缝 1 移动被触及
9. delegate/子代理：child session 语义、worktree 隔离、background、进度、token 用量上报
10. background：job 生命周期、通知注入、容量限制
11. `test_mcp_integration.py` 的既有环境失败（`mcp.server.fastmcp` 缺失）与本重构无关，不修

## 8. 非目标

- 不重构 `BackgroundJobManager`、`worktree.py`、`context/`、`providers/`、`permissions/`、`tools/` 内部
- 不做 `AgentSession` 的深度拆分（skills、持久化、记忆出 session）——只移走权限部分
- 不改 TUI 行为；`app/` 其他模块只在组装根处增负
- 不修 `test_mcp_integration.py` 环境问题
- 不改变 provider/context 层的任何对外接口
- subagent 侧保留不动：叶子包 `lanscoder/subagent/` 的放置/命名（§3）；`runner.run()` 的同步阻塞不改 async（§1.6，契约测试钉住）；`app/subagent_panel_state.py` 与 TUI 面板不动

## 9. 实施顺序（每步保持套件绿）

依赖关系决定了顺序。**步骤 1 就引入共享构造函数**，解决"组装根到步骤 5 才存在、被移出的协作对象在步骤 1-4 由谁构造"的问题：新增 `lanscoder/agent/_builders.py`（`create_agent_loop(...)`）作为唯一构造路径，每步新移出的协作对象先由它构造、经 `make_loop()` 注入 loop；步骤 5 把该函数上移到组装根、替换 runtime 的构造点。`make_loop()` fixture = `create_agent_loop(**defaults)`，测试不复制接线；契约测试断言"生产组装根与 make_loop 走同一构造函数"。

```
1. 缝 1  RequestBuilder（纯，地基；引入 _builders.py 唯一构造路径）
2. 缝 2  TurnGuardrails（纯策略）
3. 缝 7  工具注册上移（缝 5 的前置）
4. 缝 6  TurnObserver + McpActivationTracker（缝 5 完整注入的前置；只收敛事件三回调，foreground 查询留 loop）
5. 缝 5  断环 + 去双树 + 命名消歧 + 完整组装根 + 公共 API 重塑 + make_loop() fixture（核心手术，契约测试全量落位；5a/5b/5d 在此步，_builders 上移组装根）
6. 缝 3  PermissionResumeHandler（依赖缝 1 减负后的 loop）
7. 缝 4  PermissionCoordinator + child 权限归口（5c 子项在此步完成，涉及面最广，等环断了再动）
```

每步结束：`ruff` + `black --check` + 受影响测试 + 全量套件绿。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 缝 5 重塑构造签名，130 处测试直接构造被打破 | 收敛为 `make_loop()` fixture；契约测试在改造前先钉死当前行为，改造后验证等价 |
| 缝 3 的判别式接口设计不当，把 loop 状态机逻辑漏进 handler | 契约测试钉死 `ResumeOutcome` 的判别式；handler 只做纯部分 |
| 缝 4 权限收敛涉及面广，行为回归 | 最后一个做；现有权限测试 + 新增协调器契约测试守护 |
| 缝 5 的 runner 工具列表顺序依赖（background 工具先注册） | 缝 7 先做，registry 完备先于 loop 存在 |
| 缝 5b 去双树破坏 run-tail 行为等价 | 先抽共用收割逻辑、后删重复；delegate/子代理既有测试全量守护 |
| 缝 5d 改名波及 import 点（loop.py、组装根、测试） | 机械改名 + 契约测试钉新名；`grep SubagentRunner` 清零 |
| 缝 5 的 5c 子项（随缝 4 在步骤 7 完成）child 权限 3 路径 → 1，行为不等价 | 三条路径的 grant 差异（inline 加 NETWORK、isolated 加 write-tree、background 加 AGGRESSIVE）在协调器里逐一保留，权限测试逐路径守护 |
| 重构中混入行为改动 | 每步只做移动不改变逻辑；契约测试先钉行为、后验证等价 |
