# Agent 架构合理化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务实现。步骤用 `- [ ]` 语法。

**Goal:** 把 `AgentLoop`（1330 行 / 67 方法）从 god class 拆成 7 个协作对象（RequestBuilder / TurnGuardrails / PermissionResumeHandler / PermissionCoordinator / TurnObserver / McpActivationTracker / SubagentEngine），模块级 import 图无环，装配全部上移组装根，每个缝配双向契约测试，任一方违约即爆红。
**Architecture:** 拆 7 缝并按 §9 顺序推进：纯变换（缝 1）→ 纯策略（缝 2）→ 注册上移（缝 7）→ 观测收敛（缝 6）→ subagent 合理化 + 完整注入 + make_loop（缝 5）→ 权限恢复子流程（缝 3）→ 权限领域收敛（缝 4）。`_builders.py` 在步骤 1 作为唯一构造路径引入，步骤 5 上移组装根 `app/runtime.py`；每步移出的协作对象先由它构造、经 `make_loop()` 注入 loop，测试不复制接线。
**Tech Stack:** Python 3.11+ / anyio / pytest / ruff / black
**Spec:** docs/superpowers/specs/2026-08-20-agent-architecture-rationalization-design.md

## Global Constraints

从 spec §3/§7/§8 提炼的硬约束，每条任务必须满足：

- **7 缝全做**（spec §5）：缝 1 RequestBuilder / 缝 2 TurnGuardrails / 缝 7 工具注册上移 / 缝 6 TurnObserver + McpActivationTracker / 缝 5 subagent 合理化（5a 断环 / 5b 去双树 / 5c 权限归口并入缝 4 / 5d 命名消歧）/ 缝 3 PermissionResumeHandler / 缝 4 PermissionCoordinator。顺序固定为 §9：1 → 2 → 7 → 6 → 5 → 3 → 4。
- **完整注入**（spec §3）：`tool_executor`/`observer`/`request_builder`/`guardrails`/`permission_resume`/`subagent_runner` 全部由组装根构造注入，`AgentLoop` 变纯运行器，`__init__` 不再注册任何工具、不再构造任何协作对象。
- **公共 API 可重塑 + 契约测试**（spec §3/§6）：每个缝的双侧（消费者 + 提供者）都配契约测试，签名/协议/顺序任一违约即爆红。契约测试落在 `tests/test_agent_contracts.py`（新增）。
- **三个 sync facade 保留**（spec §5 缝 5、P1-2）：`_run_user_turn_sync`/`_run_nudge_turn_sync`/`_resume_with_user_input_sync`（loop.py:248/293/343）是约 120 处测试的同步入口，**保留为薄 facade**（`asyncio.run(内部 async 核心)`），不改行为、不删，加契约测试钉住。
- **subagent_engine.py 改名**（spec §5 5d）：`lanscoder/agent/subagent.py` → `lanscoder/agent/subagent_engine.py`，具体类 `SubagentRunner` → `SubagentEngine`；叶子 `lanscoder/subagent/types.py` 的 `SubagentRunner` Protocol 保留原名并显式化 `foreground_progress` 成员。
- **loop 删 `tools` 参数**（spec 缝 7）：注册上移组装根后 `tools` 变为死输入；最终在缝 5 从 `AgentLoop.__init__` 移除（子代理 child 构造在缝 5 前依赖其兼容性，见任务 3 说明）。
- **非目标不碰的文件**（spec §8）：`BackgroundJobManager`、`worktree.py`、`context/`、`providers/`、`permissions/`、`tools/` 内部；`AgentSession` 深度拆分（skills/持久化/记忆出 session）不做，只移权限部分；TUI 行为不改，`app/subagent_panel_state.py` 不动，`app/` 其他模块只在组装根增负；`test_mcp_integration.py` 的既有环境失败（`mcp.server.fastmcp` 缺失）不修；provider/context 层对外接口不变；`lanscoder/subagent/` 叶子包放置/命名不变；`runner.run()` 同步阻塞不改 async（契约测试钉住）。
- **每步套件绿**（spec §9）：每步结束 `ruff check` + `black --check` + 受影响测试；任务末尾全量 `python -m pytest -q`，预期 **1400 passed / 1 skipped / 1 failed**（唯一失败是既有 MCP 环境问题 `test_mcp_integration.py`）。
- **no-subagent 契约**：本计划执行期间实现者**不得派子 agent** 完成任何任务；7 个任务全部由同一实现者按顺序完成。
- **Git 纪律**（CLAUDE.md + 框架要求）：每任务末尾显式路径 `git add <path1> <path2>` + 单条 commit（消息格式 `{feat,fix,docs}: <imperative>`，尾部 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`）；**绝不 `git add -A` / `git add .`**；commit 前 `git status` 核对只含本任务文件；不 commit 未测绿的任务。

---

### Task 1: 缝 1 — RequestBuilder（纯变换 + `_builders.py` 唯一构造路径）

**Files:**
- Create: `lanscoder/agent/request_builder.py`
- Create: `lanscoder/agent/_builders.py`
- Modify: `lanscoder/agent/loop.py`
- Modify: `lanscoder/app/runtime.py`
- Create: `tests/test_agent_contracts.py`

**Interfaces:**
- Consumes: `PreparedMainRequest`（loop.py:83-88，本任务迁入 request_builder.py 后由 loop 回 import）、`ContextBudget`/`build_context_budget`（lanscoder.context.token_budget）、`ChatMessage`/`ChatRequest`/`MainRequestOptions`（providers.types）、`ContextBuilder`、`AgentSession.build_system_prefix`/`store.root`、`new_request_id`/`stable_json_hash`（context.identity）、`render_current_task_plan_snapshot`（task_plan_policy）、`ChatProvider`。
- Produces（后续任务依赖）:
  ```python
  class RequestBuilder:
      def __init__(self, *, session: AgentSession, provider: ChatProvider, context_builder: ContextBuilder,
                   request_options: MainRequestOptions, context_window: int | None) -> None: ...
      def build(self, view, *, definitions, tool_choice="auto", runtime_instruction=None) -> PreparedMainRequest
      def context_budget_for_view(self, view, *, runtime_instruction, definitions) -> ContextBudget
  ```
  `lanscoder/agent/_builders.py::create_agent_loop(**kwargs) -> AgentLoop`（唯一构造路径，任务 3/4/6/7 在此增负，任务 5 上移组装根）。

**关键设计说明**：spec 缝 1 的构造依赖列了 session/context_builder/request_options/context_window 四项，但被移入的 `_request_messages` 读 `provider.name/model/capabilities`（loop.py:1077-1079，构建 system prefix）。故 `RequestBuilder.__init__` 注入 **provider** 作为第 5 个依赖，否则移入的代码无 `self.provider` 无法编译。接口（`build`/`context_budget_for_view`）不变，`provider` 只是构造依赖。

- [ ] **Step 1: 创建 `lanscoder/agent/request_builder.py`。**
  1. 把 `PreparedMainRequest` dataclass（loop.py:83-88，frozen+slots，字段 request/request_id/projection_fingerprint/tool_result_part_ids）整体迁入本文件。
  2. 新增 `RequestBuilder`，从 loop.py 平移 4 个方法的 body（签名按下面改）：
     - `_build_provider_messages(self, view, *, system_prefix)`（loop.py:1067-1072，改读 `self.session.store.root`）。
     - `_request_messages(self, *, view, runtime_instruction)`（loop.py:1074-1097，**view 改为必传 keyword-only，删除 `view or self.session.rebuild_view()` 内嵌重建**；`self.provider` 改 `self._provider`）。
     - `_main_chat_request(self, messages, definitions, tool_choice)`（loop.py:626-632，改读 `self._request_options`）。
     - `_context_budget_for_view` → 公共 `context_budget_for_view(self, view, *, runtime_instruction, definitions)`（loop.py:1000-1016，`self.context_window`/`self._request_options.max_tokens`）。
  3. `build(self, view, *, definitions, tool_choice="auto", runtime_instruction=None) -> PreparedMainRequest`：body 是 loop.py:948-964 的 `_request_messages` + `_main_chat_request` + `PreparedMainRequest(...)`（`new_request_id()` / `stable_json_hash(...)` / `self.context_builder.projected_tool_result_part_ids(view)`）。
  4. import 依赖：`AgentSession` 用 `TYPE_CHECKING` 下 `from lanscoder.agent.session import AgentSession`；`ChatProvider`/`ChatMessage`/`ChatRequest`/`MainRequestOptions`、`ContextBudget`/`build_context_budget`、`ContextBuilder`、`new_request_id`/`stable_json_hash`、`render_current_task_plan_snapshot`。
- [ ] **Step 2: 修改 `lanscoder/agent/loop.py` —— 删方法、接 builder、加 `_loop_tool_definitions`。**
  1. 顶部 `from lanscoder.agent.request_builder import PreparedMainRequest, RequestBuilder`；删除本文件的 `PreparedMainRequest` dataclass 定义（:83-88）。
  2. 删除方法：`_build_provider_messages`（:1067）、`_request_messages`（:1074）、`_main_chat_request`（:626）、`_context_budget_for_view`（:1000）、`context_budget_for_view`（:1018）。
  3. `__init__` 加参数 `request_builder: RequestBuilder | None = None`；为 None 时兜底构造 `RequestBuilder(session=session, provider=provider, context_builder=context_builder or ContextBuilder(), request_options=request_options or MainRequestOptions(), context_window=context_window)`，存 `self.request_builder`。
  4. `_prepare_main_provider_request`（:913-964）：把 `messages = self._request_messages(...)` + `request = self._main_chat_request(...)` + `return PreparedMainRequest(...)` 整段替换为 `return self.request_builder.build(view, definitions=definitions, tool_choice=tool_choice, runtime_instruction=runtime_instruction)`；把两处 `budget = self._context_budget_for_view(...)`（:925、:936 lambda 内）替换为 `self.request_builder.context_budget_for_view(...)`。
  5. `_compact_if_needed`（:1041、:1051）两处 `_context_budget_for_view` 同样替换为 `self.request_builder.context_budget_for_view(...)`。
  6. 新增公共薄访问器：`def _loop_tool_definitions(self): return self._provider_tool_definitions()`（spec 缝 1：runtime 依赖它，避免 runtime 再造 augment 逻辑）。
- [ ] **Step 3: 创建 `lanscoder/agent/_builders.py`（唯一构造路径）。**
  ```python
  def create_agent_loop(**kwargs) -> AgentLoop:
      request_builder = RequestBuilder(
          session=kwargs["session"],
          provider=kwargs.get("provider"),
          context_builder=kwargs.get("context_builder"),
          request_options=kwargs.get("request_options"),
          context_window=kwargs.get("context_window"),
      )
      return AgentLoop(**kwargs, request_builder=request_builder)
  ```
  顶部 import `AgentLoop`/`RequestBuilder`/`ContextBuilder`/`MainRequestOptions`。后续任务在此函数内逐步构造更多协作对象并注入。
- [ ] **Step 4: 修改 `lanscoder/app/runtime.py` —— `context_budget` 改法 + 空 loop fallback。**
  1. `AgentChatRunner` dataclass 加字段：`loop: AgentLoop | None = None`、`request_builder: RequestBuilder | None = None`。
  2. `_create_loop`（:387-408）：`loop = AgentLoop(**kwargs)` → `loop = create_agent_loop(**kwargs)`；随后 `self.loop = loop`、`self.request_builder = loop.request_builder`。顶部 `from lanscoder.agent._builders import create_agent_loop`。
  3. `context_budget`（:376-378）替换为：
     ```python
     def context_budget(self, view):
         builder = self.request_builder
         if builder is None:
             builder = RequestBuilder(
                 session=self.current_session.session, provider=self.provider,
                 context_builder=self.context_builder or ContextBuilder(),
                 request_options=self.request_options or MainRequestOptions(),
                 context_window=self.context_window,
             )
             self.request_builder = builder
         definitions = self.loop._loop_tool_definitions() if self.loop is not None else self._registry_tool_definitions()
         return builder.context_budget_for_view(view, runtime_instruction=None, definitions=definitions)
     ```
     （不再构造即弃 loop；`self.loops` 列表仅作调试历史保留，不再被 `context_budget` 读取。）
  4. 新增 `_registry_tool_definitions(self)`：`[d for d in self.current_session.session.tool_registry.definitions() if d.name not in HIDDEN_TOOL_STATUS_NAMES]`，注释注明 MCP 激活过滤在缝 6 引入 McpActivationTracker 后由组装根接入。
- [ ] **Step 5: 契约测试（`tests/test_agent_contracts.py`，新建文件）。**
  1. `test_request_builder_build_contract_signature`：
     ```python
     params = list(inspect.signature(RequestBuilder.build).parameters)
     assert params[0] == "self" and params[1] == "view" and "definitions" in params
     assert inspect.signature(RequestBuilder.build).parameters["definitions"].kind == inspect.Parameter.KEYWORD_ONLY
     bparams = list(inspect.signature(RequestBuilder.context_budget_for_view).parameters)
     assert bparams[1] == "view" and "runtime_instruction" in bparams and "definitions" in bparams
     ```
  2. `test_request_builder_build_pure_behavior`：构造 session + FakeProvider + RequestBuilder（四个注入依赖），`build(view, definitions=[d1])` 返回 `PreparedMainRequest`，断言 `request.messages` 由 view 投影、`request.tools == [d1]`、`tool_result_part_ids == context_builder.projected_tool_result_part_ids(view)`、`request_id`/`projection_fingerprint` 非空——零 loop 状态、独立可测。
  3. 文件头部 import `inspect`、`RequestBuilder`、`PreparedMainRequest`（自 `lanscoder.agent.request_builder`）。
- [ ] **Step 6: 验证 + 提交。**
  运行 `python -m pytest tests/test_agent_contracts.py tests/test_agent_context_loop.py tests/test_model_request_options.py tests/test_app_runtime.py -q`；`ruff check` + `black --check` 修改文件。全量 `python -m pytest -q`（预期 1400/1/1）。
  `git add lanscoder/agent/request_builder.py lanscoder/agent/_builders.py lanscoder/agent/loop.py lanscoder/app/runtime.py tests/test_agent_contracts.py` + commit `refactor: extract RequestBuilder with unique _builders construction path`

---

### Task 2: 缝 2 — TurnGuardrails（纯策略 + 计数器归属）

**Files:**
- Create: `lanscoder/agent/guardrails.py`
- Modify: `lanscoder/agent/loop_limits.py`（移入 `_AgentLoopLimitReached`）
- Modify: `lanscoder/agent/loop.py`
- Modify: `tests/test_agent_contracts.py`
- Modify: `tests/test_model_request_options.py`（:189）
- Modify: `tests/test_agent_context_loop.py`（:2490 附近 + 其余 `provider_call_count` 引用）

**Interfaces:**
- Consumes: `AgentLoopStopReason`（loop_limits，已存在）、`AgentLoopLimits`、`ChatProvider`（name/model）、`clock`。
- Produces（任务 4/5 依赖）:
  ```python
  class TurnGuardrails:
      def __init__(self, *, provider: ChatProvider, limits: AgentLoopLimits, clock=time.monotonic) -> None: ...
      def begin_turn(self) -> None              # 重置 provider_call_count=0、turn_started_at=clock()
      def reserve_call(self) -> None            # 抛 _AgentLoopLimitReached
      def check_timeout(self) -> None           # 抛 _AgentLoopLimitReached
      @property
      def call_count(self) -> int               # 只读访问器，_report_progress/usage_summary 读它
      def limit_response(self, reason: AgentLoopStopReason, *, raw: dict | None = None) -> ChatResponse
      def interrupted_response(self) -> ChatResponse
  ```
  `_AgentLoopLimitReached` 移入 `loop_limits.py`（数据层；被异常、guardrails、loop 出口三方使用）。

- [ ] **Step 1: 修改 `lanscoder/agent/loop_limits.py`。** 把 `_AgentLoopLimitReached`（loop.py:1327-1330）整个移入；删除 loop.py 中该 class 定义。`AgentLoopStopReason` 已在本文件。
- [ ] **Step 2: 创建 `lanscoder/agent/guardrails.py`。** `TurnGuardrails` 按上面接口；`_provider_call_count`/`_turn_started_at` 由本类持有；`limit_response` body 平移自 loop.py:1293-1306（`self._limits`/`self._provider`）；`interrupted_response` 平移自 :1308-1316。`reserve_call` = `_check_provider_call_limit` + 自增（loop.py:1237-1244 合并）；`check_timeout` = loop.py:1246-1251。
- [ ] **Step 3: 修改 `lanscoder/agent/loop.py` —— 接 guardrails、删方法。**
  1. `__init__` 加 `guardrails: TurnGuardrails | None = None`；为 None 兜底 `TurnGuardrails(provider=provider, limits=limits or AgentLoopLimits.default(), clock=clock)`，存 `self.guardrails`。删除实例属性 `self.provider_call_count`/`self.turn_started_at`（:152-153）。
  2. 删除方法：`_check_provider_call_limit`（:1237）、`_reserve_provider_call`（:1242）、`_check_turn_timeout`（:1246）、`_limit_response`（:1293）、`_interrupted_response`（:1308）。
  3. 替换调用点：
     - `_complete_once`: `self._reserve_provider_call()`（:650）→ `self.guardrails.reserve_call()`；`self._check_turn_timeout()`（:651）→ `self.guardrails.check_timeout()`。
     - `_resume_with_user_input_async`/`_streaming`: `self._check_turn_timeout()`（:365、:385）→ `self.guardrails.check_timeout()`。
     - `_run_tool_loop`（:751/:770）、`_continue_tool_loop_from_response`（:839/:849）: `self._limit_response(...)` → `self.guardrails.limit_response(...)`。
     - `_tool_round_limit_response`（:1288-1291）保留为 loop glue，body 改 `return self.guardrails.limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT, raw=response.raw)`。
  4. `_begin_turn`（:1174-1180）：`if new_user_turn:` 内 `self.provider_call_count = 0` + `self.turn_started_at = self.clock()` → `self.guardrails.begin_turn()`（其余 `_active_mcp_tool_names.clear()`/`_task_plan_reconciliation_attempted`/`_tool_rounds_completed` 保留，`_active_mcp_tool_names` 清理在任务 4 移入 tracker）。
  5. `_report_progress`（:975-991）：`"provider_calls": self.provider_call_count` → `"provider_calls": self.guardrails.call_count`。`usage_summary`（:993-998）：同样改 `self.guardrails.call_count`。
  6. import `TurnGuardrails` 自 guardrails；`_AgentLoopLimitReached` 改自 loop_limits import。
- [ ] **Step 4: 迁移既有计数断言。**
  `tests/test_model_request_options.py:189` `assert loop.provider_call_count == 2` → `assert loop.guardrails.call_count == 2`；`tests/test_agent_context_loop.py:2490` `test_agent_loop_resets_provider_call_count_for_each_user_turn` 改为读 `loop.guardrails.call_count`（每轮后断言 0）。`grep -rn "provider_call_count\|turn_started_at" tests/` 清零其余引用。
- [ ] **Step 5: 契约测试（`tests/test_agent_contracts.py`）。**
  1. `test_turn_guardrails_contract_signature`：`inspect.signature(TurnGuardrails.reserve_call).parameters == ["self"]`；`check_timeout`/`begin_turn` 同；`limit_response` 参数含 `reason`；`isinstance(guardrails.call_count, int)`。
  2. `test_turn_guardrails_behavior`：`limits=AgentLoopLimits(max_provider_calls=1)`，第一次 `reserve_call()` 通过、`call_count==1`，第二次 `pytest.raises(_AgentLoopLimitReached)`；`begin_turn()` 后 `call_count==0`；fake clock（`clock=lambda: 100.0` → 改 110.0）+ `limits=max_turn_seconds=5` 下 `check_timeout()` 抛。
- [ ] **Step 6: 验证 + 提交。**
  `python -m pytest tests/test_agent_contracts.py tests/test_agent_context_loop.py tests/test_model_request_options.py tests/test_agent_loop_limits.py -q`；`ruff` + `black --check`；全量 `python -m pytest -q`。
  `git add lanscoder/agent/guardrails.py lanscoder/agent/loop_limits.py lanscoder/agent/loop.py tests/test_agent_contracts.py tests/test_model_request_options.py tests/test_agent_context_loop.py` + commit `refactor: extract TurnGuardrails with turn counter ownership`

---

### Task 3: 缝 7 — 工具注册上移（组合根，registry 单一源）

**Files:**
- Modify: `lanscoder/agent/loop.py`（删 `_ensure_background_control_tools`/`_ensure_delegate_tool`、停止注册 caller tools、加 `subagent_runner` 注入、删模块级 subagent/delegate/background 控制工具 import）
- Modify: `lanscoder/agent/_builders.py`（加 `register_loop_tools`，create_agent_loop 内注册）
- Modify: `tests/test_agent_context_loop.py`（6 处 `tools=` 构造 → `create_agent_loop`）
- Modify: `tests/test_background_jobs.py`（:1147 → `create_agent_loop`）
- Modify: `tests/test_delegate_tool.py`（:168/:188 → `create_agent_loop`）
- Modify: `tests/test_agent_contracts.py`（`test_registry_complete_before_loop`）
- Modify: `lanscoder/app/runtime.py`（无代码改动，仅确认 `_create_loop` 已走 create_agent_loop 且传 `tools=self._current_tools()`）

**Interfaces:**
- Consumes: `SubagentRunner` 叶子 Protocol（lanscoder.subagent.types）、`create_delegate_tool`（tools.delegate）、`create_background_status_tool`/`create_background_cancel_tool`（tools.background）、`session.tool_registry`。
- Produces: `_builders.register_loop_tools(...)`、`AgentLoop.__init__` 新增 `subagent_runner` 参数。

**顺序依赖说明**：`AgentLoop.__init__` 的 `tools`/`enable_delegate_tool`/`progress_callback` 参数本任务**暂不移除**——`lanscoder/agent/subagent.py` 的 `_run_inline`/`_run_isolated` 仍在直构 child loop（缝 5 才改工厂），它们传 `enable_delegate_tool=False`/`background_manager=None`/`progress_callback=...`；传 `tools=` 的注册在 child 路径实为 no-op（child session 的 registry 已由 `AgentSession.create(tools=...)` 填好，见 subagent.py:383-391/:447-456，`retrieve_archive` 由 `create_session_tool_registry` 注入）。三参数在任务 5 与 make_loop 收敛一并移除。

- [ ] **Step 1: 修改 `lanscoder/agent/loop.py` —— loop 不再注册任何工具，只收引用。**
  1. 删除 `__init__` 阶段 6 的 caller-tools 注册循环（:177-180）与 `self._mcp_tool_names` 派生（:181，移入 tracker 见任务 4）；删除阶段 8 的 `self._ensure_background_control_tools()` / `self._ensure_delegate_tool()` 调用（:202-203）。
  2. 删除方法 `_ensure_background_control_tools`（:1127）、`_ensure_delegate_tool`（:1138）。
  3. 删除模块级 import：`from lanscoder.agent.subagent import SubagentRunner`（:67）、`create_background_cancel_tool`/`create_background_status_tool`（:66）、`create_delegate_tool`（:68）。保留 `DEFAULT_BACKGROUND_TOOL_NAMES`/`with_background_controls`（`_augment_tool_definition` 仍用）。
  4. `__init__` 加 `subagent_runner: SubagentRunner | None = None`（叶子 Protocol，`from lanscoder.subagent.types import SubagentRunner`）；删 `self._delegate_runner`（:170），`foreground_subagent`（:1168-1172）改为：
     ```python
     def foreground_subagent(self) -> dict[str, Any] | None:
         if self._subagent_runner is None:
             return None
         return self._subagent_runner.foreground_progress
     ```
  5. 保留 `tools`/`enable_delegate_tool`/`progress_callback` 参数（任务 5 移除），但不再使用（加 `# noqa` 不需要——参数未被引用即可；ruff 不检查未用参数）。
- [ ] **Step 2: 修改 `lanscoder/agent/_builders.py` —— 注册上移组装根。**
  1. 新增：
     ```python
     def register_loop_tools(session, *, caller_tools, background_manager, provider,
                             request_options, subagent_runner=None) -> SubagentRunner | None:
         registry = session.tool_registry
         for tool in caller_tools or []:
             if tool.name not in registry.names():
                 registry.register(tool)
         if background_manager is not None:
             if "background_status" not in registry.names():
                 registry.register(create_background_status_tool(background_manager, session_id=session.session_id))
             if "background_cancel" not in registry.names():
                 registry.register(create_background_cancel_tool(background_manager, session_id=session.session_id))
         if subagent_runner is None:
             project_root = session.permission_manager.policy.project_root if session.permission_manager is not None else None
             subagent_runner = SubagentRunner(
                 store=session.store, provider=provider,
                 tools=[t for t in registry.tools() if t.name != "delegate"],
                 project_root=project_root, agents_md=session.agents_md,
                 skill_catalog=session.skill_catalog,
                 permission_manager=session.permission_manager,
                 sandbox_access=session.sandbox_access,
                 request_options=request_options, background_manager=background_manager,
             )
         if "delegate" not in registry.names():
             registry.register(create_delegate_tool(subagent_runner, parent_session_id=session.session_id))
         return subagent_runner
     ```
     （body 镜像原 loop.py:1131-1166 的 dedup 注册。）
  2. `create_agent_loop(**kwargs)`：构造 RequestBuilder 后、构造 AgentLoop 前，调
     `runner = register_loop_tools(kwargs["session"], caller_tools=kwargs.get("tools"), background_manager=kwargs.get("background_manager"), provider=kwargs.get("provider"), request_options=kwargs.get("request_options"))`，并 `kwargs = {**kwargs, "subagent_runner": runner}`。import `SubagentRunner`/`create_delegate_tool`/`create_background_status_tool`/`create_background_cancel_tool`。
- [ ] **Step 3: 确认 `lanscoder/app/runtime.py`。** `_create_loop` 已传 `"tools": self._current_tools()`（:395）且走 `create_agent_loop`（任务 1）→ 无需改动。跑绿证明注册生效。
- [ ] **Step 4: 迁移受影响测试。**
  1. `tests/test_agent_context_loop.py` 6 处 `AgentLoop(session=..., provider=..., tools=[...])`（:598/:628/:814/:862/:899/:2742）→ `create_agent_loop(session=..., provider=..., tools=[...])`；文件顶部 `from lanscoder.agent._builders import create_agent_loop`。
  2. `tests/test_background_jobs.py:1147` `test_agent_loop_registers_background_control_tools_for_custom_tool_sets`：`AgentLoop(...)` → `create_agent_loop(session=session, provider=..., background_manager=manager)`（断言不变：`background_status`/`background_cancel` 在 registry、shell 定义含 `run_in_background`、background_status 不含）。
  3. `tests/test_delegate_tool.py:168` `test_agent_loop_registers_delegate_and_foreground_returns_summary` 与 `:188` `test_foreground_delegate_result_includes_usage_and_elapsed`：`AgentLoop(session, provider)` → `create_agent_loop(session=session, provider=provider)`（delegate 由 register_loop_tools 注册，断言不变）。文件顶部 import 调整。
- [ ] **Step 5: 契约测试 `test_registry_complete_before_loop`（`tests/test_agent_contracts.py`）。**
  - 构造 session + FakeProvider + BackgroundJobManager；调 `register_loop_tools(session, caller_tools=[caller_tool], background_manager=manager, provider=provider, request_options=MainRequestOptions())`；
  - 断言在构造 AgentLoop **之前** registry 已含 caller_tool + `"background_status"` + `"background_cancel"` + `"delegate"`；
  - 断言 `_current_tools()` 喂入的每个工具已在 registry（单一工具源）；
  - spy `session.tool_registry.register`，构造 `AgentLoop(session=session, provider=provider, ...)`，断言构造期间 register 零新增（注册完全上移）。
- [ ] **Step 6: 验证 + 提交。**
  `python -m pytest tests/test_agent_contracts.py tests/test_agent_context_loop.py tests/test_background_jobs.py tests/test_delegate_tool.py -q`；`ruff` + `black --check`；全量 `python -m pytest -q`。
  `git add lanscoder/agent/loop.py lanscoder/agent/_builders.py tests/test_agent_context_loop.py tests/test_background_jobs.py tests/test_delegate_tool.py tests/test_agent_contracts.py` + commit `refactor: move tool registration to assembly root with single registry source`

---

### Task 4: 缝 6 — TurnObserver + McpActivationTracker（观测收敛 + 解构造环）

**Files:**
- Create: `lanscoder/agent/observer.py`（`TurnObserver`、`ToolEventSink`）
- Create: `lanscoder/agent/mcp_activation.py`（`McpActivationTracker`）
- Modify: `lanscoder/agent/tool_execution.py`（`ToolEventSink` 收窄、去 `check_cancelled` 参数、内部 None-guard）
- Modify: `lanscoder/agent/loop.py`（observer/tracker/tool_executor 注入 + 兜底；事件走 observer；`_begin_turn` 调 `tracker.clear()`；`_provider_tool_definitions` 读 `tracker.active_names`；handler 透传 setter；`replace_cancellation_token` 重绑；保留 `tool_executor` 属性）
- Modify: `lanscoder/agent/_builders.py`（构造 tracker/observer/tool_executor 注入）
- Modify: `tests/test_agent_contracts.py`（4 个契约测试）

**Interfaces:**
- Consumes: `ToolExecutionEvent`（tool_execution）、`ChatStreamEvent`、`ToolCall`/`ToolResult`、`make_error_result`、`CancellationToken`。
- Produces（任务 5 依赖）:
  ```python
  # observer.py
  class ToolEventSink(Protocol):
      def on_tool_event(self, event: ToolExecutionEvent) -> None: ...
  class TurnObserver:
      def __init__(self, *, stream_event_handler=None, tool_event_handler=None, progress_callback=None,
                   foreground_progress_provider=None, cancellation_token=None) -> None: ...
      def on_turn_started(self) -> None: ...
      def on_progress(self, provider_calls: int, total_tokens: int) -> None: ...
      def on_tool_event(self, event: ToolExecutionEvent) -> None: ...
      def on_stream_event(self, event: ChatStreamEvent) -> None: ...
      def foreground_progress(self) -> dict[str, Any] | None: ...
      def usage_summary(self) -> dict[str, int]: ...
      def set_stream_event_handler(self, handler) -> None: ...
      def set_tool_event_handler(self, handler) -> None: ...
      def replace_cancellation_token(self, token: CancellationToken | None) -> None: ...
  # mcp_activation.py
  class McpActivationTracker:
      def __init__(self, mcp_tool_names: frozenset[str]) -> None: ...
      def clear(self) -> None                        # 原 _begin_turn 的清空
      def validate(self, tool_call: ToolCall) -> ToolResult | None
      def observe(self, tool_call: ToolCall, result: ToolResult) -> None
      @property
      def active_names(self) -> frozenset[str]: ...
      @property
      def mcp_tool_names(self) -> frozenset[str]: ...
  ```

**本任务范围**：只收敛三个事件回调（progress_callback/tool_event_handler/stream_event_handler）到 observer；`foreground_progress` 查询暂留 loop 读 `_subagent_runner`（P1-5），任务 5 换 observer 的 provider。

- [ ] **Step 1: 创建 `lanscoder/agent/observer.py`。** `ToolEventSink` Protocol + `TurnObserver` 按上面接口。默认实现（**用量由 observer 从 `on_progress` 镜像，避免构造期回引 loop**）：
  - `on_progress(provider_calls, total_tokens)`：存 `self._provider_calls = provider_calls`、`self._total_tokens = total_tokens`（`total_tokens` 是 loop `_report_progress` 累积后的运行值），仅当 `progress_callback` 非空时调 `progress_callback({"provider_calls": provider_calls, "total_tokens": total_tokens})`。
  - `usage_summary()` 返回 `{"provider_calls": self._provider_calls, "total_tokens": self._total_tokens}`（初始 0/0——子代理失败路径在首次 on_progress 前调用返回 0/0，语义与现状 `loop.usage_summary()` 等价）。
  - `on_tool_event`/`on_stream_event` 对应 handler 转发；`foreground_progress()` 走 `foreground_progress_provider`（None 则返回 None）；`on_turn_started()` 空实现；`set_stream_event_handler`/`set_tool_event_handler`/`replace_cancellation_token` 更新内部字段。
- [ ] **Step 2: 创建 `lanscoder/agent/mcp_activation.py`。** `McpActivationTracker` 纯逻辑平移自 loop.py:1182-1206（`validate`/`observe`/`clear`/`active_names`），新增 `mcp_tool_names` 只读属性。
- [ ] **Step 3: 修改 `lanscoder/agent/tool_execution.py`。**
  1. `ToolExecutor.__init__`：删 `emit_event`/`check_cancelled` 参数，加 `event_sink: ToolEventSink`（必需）；保留 `cancellation_token`/`validate_tool_call`/`observe_tool_result`/`background_manager`/`background_tool_names`。删 `self._check_cancelled` 注入存储，改内部方法：
     ```python
     def _check_cancelled(self) -> None:
         if self.cancellation_token is not None:
             self.cancellation_token.raise_if_cancelled()
     ```
  2. 内部 `_emit_event(kind, tool_call, *, result=None, permission_request=None, prewrite_review=None)` 改 body 为 `self._event_sink.on_tool_event(ToolExecutionEvent(kind=kind, tool_call=tool_call, result=result, permission_request=permission_request, prewrite_review=prewrite_review))`——其余 8 处调用点（:147/:156/:162/:172/:322/:524/:529/:543）不变。
- [ ] **Step 4: 修改 `lanscoder/agent/loop.py`。**
  1. `__init__` 加参数：`observer: TurnObserver | None = None`、`mcp_activation: McpActivationTracker | None = None`、`tool_executor: ToolExecutor | None = None`。
  2. 兜底构造（供任务 5 之前直接构造的测试保持绿；任务 5 后改必需注入、删兜底）：
     - tracker：`McpActivationTracker(frozenset(n for n in session.tool_registry.names() if n.startswith("mcp__")))`，存 `self._mcp_activation`；删 `self._mcp_tool_names`/`self._active_mcp_tool_names`（:181-182）。
     - observer：`TurnObserver(stream_event_handler=stream_event_handler, tool_event_handler=tool_event_handler, progress_callback=progress_callback)`，存 `self._observer`（用量由 `on_progress` 镜像，见 Step 1）。
     - tool_executor：`ToolExecutor(session=session, event_sink=self._observer, cancellation_token=self.cancellation_token, validate_tool_call=self._mcp_activation.validate, observe_tool_result=self._mcp_activation.observe, background_manager=self.background_manager, background_tool_names=self.background_tool_names)`，存 `self.tool_executor`（保留公开属性，P2-10）。
  3. 删除方法 `_validate_mcp_tool_call`（:1182）、`_observe_mcp_search_result`（:1193）。`_provider_tool_definitions`（:1099-1112）：`if definition.name in self._mcp_tool_names and definition.name not in self._active_mcp_tool_names:` → `if definition.name in self._mcp_activation.mcp_tool_names and definition.name not in self._mcp_activation.active_names:`。
  4. `_begin_turn`：`self._active_mcp_tool_names.clear()`（:1176）→ `self._mcp_activation.clear()`。
  5. `_emit_tool_event`（:864-891）：body 改 `self._observer.on_tool_event(ToolExecutionEvent(kind=kind, tool_call=tool_call, result=result, permission_request=permission_request, prewrite_review=prewrite_review))`。
  6. `_complete_once` streaming 分支：`if self.stream_event_handler is not None: self.stream_event_handler(event)`（:662-663）→ `self._observer.on_stream_event(event)`。
  7. `_report_progress`：`if self.progress_callback is None: return; self.progress_callback({...})`（:984-991）→ `self._observer.on_progress(self.guardrails.call_count, self._total_tokens)`（`_total_tokens` 累积保留）。
  8. `usage_summary`（:993-998）→ `return self._observer.usage_summary()`。
  9. `stream_event_handler`/`tool_event_handler` 改为 property（getter 返回内部字段；setter 写内部字段并调 `self._observer.set_stream_event_handler(value)`/`set_tool_event_handler(value)`）——runtime `_resume_turn` 的 `loop.stream_event_handler = ...`/`loop.tool_event_handler = ...`（runtime.py:232-233）形状不变。
  10. `replace_cancellation_token`（:236-240）：保留并加 `self._observer.replace_cancellation_token(token)`。
  11. `foreground_subagent()` 保持读 `_subagent_runner`（本任务不动）。
- [ ] **Step 5: 修改 `lanscoder/agent/_builders.py`。** `create_agent_loop` 在构造 loop 前构造 tracker（`McpActivationTracker(frozenset(...))`）、observer（从 kwargs 的 stream_event_handler/tool_event_handler/progress_callback）、tool_executor，注入 AgentLoop（覆盖兜底）。
- [ ] **Step 6: 契约测试（`tests/test_agent_contracts.py`）。**
  1. `test_observer_consumer_pipeline`：RecordingObserver（记录 on_progress/on_tool_event/on_stream_event 调用），注入 loop，跑一次 `_run_user_turn_sync`，断言三者被调用且负载形状正确——`on_progress` 的 `provider_calls >= 1`/`total_tokens >= 0`；`on_tool_event` 收到 `ToolExecutionEvent`（kind 在合法集合）；`on_stream_event`（streaming 模式）收到 `ChatStreamEvent`。
  2. `test_mcp_activation_tracker_turn_boundary`：对齐 `test_agent_loop_clears_mcp_activation_on_next_user_turn`（tests/test_agent_context_loop.py:1173），复用同一 loop 跑两轮，断言第二轮 `_provider_tool_definitions()` 不含未激活 mcp 工具（钉死 `_begin_turn` → `tracker.clear()` 每轮接线）。
  3. `test_resume_rebind_observers`：跑一轮暂停（ask_user），`loop.stream_event_handler = new_handler`/`loop.tool_event_handler = new_handler`（透传 setter），resume，断言下一事件进入新 handler。
  4. `test_loop_tool_executor_attribute_contract`：`isinstance(loop.tool_executor, ToolExecutor)` 且 `loop.tool_executor.execute_interactive` 可调。
  5. `test_mcp_activation_tracker_contract`：`McpActivationTracker(frozenset({"mcp__a"}))`，`validate(ToolCall(name="mcp__a",...))` 返回 `mcp_activation_required=True` 的 error；`observe(mcp_tool_search 激活结果)` 后 `validate` 返回 None；`active_names` 更新。
- [ ] **Step 7: 验证 + 提交。**
  `python -m pytest tests/test_agent_contracts.py tests/test_agent_context_loop.py tests/test_background_jobs.py tests/test_delegate_tool.py tests/test_app_runtime.py -q`；`ruff` + `black --check`；全量 `python -m pytest -q`。
  `git add lanscoder/agent/observer.py lanscoder/agent/mcp_activation.py lanscoder/agent/tool_execution.py lanscoder/agent/loop.py lanscoder/agent/_builders.py tests/test_agent_contracts.py` + commit `refactor: converge event callbacks into TurnObserver with McpActivationTracker`

---

### Task 5: 缝 5 — subagent 合理化 + 完整注入 + make_loop（核心手术）

**Files:**
- Modify: `lanscoder/agent/ports.py`（`SessionTurnRunner` Protocol）
- Modify: `lanscoder/subagent/types.py`（`SubagentRunner` Protocol 显式化 `foreground_progress`）
- Modify: `lanscoder/agent/subagent.py` → `lanscoder/agent/subagent_engine.py`（`git mv` + 类改名 `SubagentEngine`）
- Modify: `lanscoder/agent/loop.py`（完整注入签名、删 `tools`/`enable_delegate_tool`/`progress_callback`/`subagent_runner` 参数、`foreground_progress()` 薄转发、sync facade 注释）
- Modify: `lanscoder/agent/_builders.py`（删除；`create_agent_loop`/`register_loop_tools` 上移 `lanscoder/app/runtime.py`）
- Modify: `lanscoder/app/runtime.py`（组装根：构造 SubagentEngine + child_runner_factory + 全协作对象）
- Modify: `tests/conftest.py`（`make_loop` fixture）
- Modify: `tests/test_agent_contracts.py`（4 个契约测试）
- Modify: `tests/test_layer_boundaries.py`（import 图无环断言）
- Modify: `tests/test_delegate_tool.py`（import `SubagentEngine`，改名波及）
- Modify: 全部直接 `AgentLoop(` 构造的测试文件（迁移到 `make_loop`）

**Interfaces:**
- Consumes: `AgentTurnResult`（agent.user_input）、`SubagentRequest`/`SubagentResult`/`SubagentProfile`（subagent.types）、`TurnObserver`、`current_cancellation_token`。
- Produces（任务 6/7 依赖）:
  ```python
  # ports.py
  class SessionTurnRunner(Protocol):
      async def run_user_turn(self, content: str) -> AgentTurnResult: ...
      def usage_summary(self) -> dict[str, int]: ...
  # app/runtime.py
  def create_agent_loop(*, session, provider, context_builder=None, context_manager=None, limits=None,
                        request_options=None, context_window=None, background_manager=None,
                        background_tool_names=None, guidance_provider=None, cancellation_token=None,
                        stream_event_handler=None, tool_event_handler=None, enable_delegate_tool=True,
                        **_) -> AgentLoop: ...   # 全协作对象构造注入；`tools` 仅用于注册，不回传 loop
  # SubagentEngine.__init__ 新增
  def __init__(self, *, store, provider, tools, project_root=None, agents_md="", skill_catalog=None,
               permission_manager=None, sandbox_access=None, request_options=None, limits=None,
               background_manager=None, child_runner_factory: Callable[..., SessionTurnRunner]) -> None: ...
  ```

**关键顺序**：本任务先做 5a/5b/5d 与 make_loop 收敛，最后做 loop 完整注入签名（删三参数 + 协作对象改必需）——因为 `subagent.py` 的 `_run_inline`/`_run_isolated` 仍直构 child loop，只有改成工厂后才可安全删 loop 参数。

- [ ] **Step 1: `ports.py` 加 `SessionTurnRunner` Protocol。** 按上面签名；`from lanscoder.agent.user_input import AgentTurnResult`（同层，无环）。
- [ ] **Step 2: `subagent/types.py` 显式化 `SubagentRunner` Protocol 成员。** 加 `foreground_progress: dict[str, Any] | None`（属性声明，`Any` 已 import）。
- [ ] **Step 3: 5d 命名消歧（机械改名）。**
  1. `git mv lanscoder/agent/subagent.py lanscoder/agent/subagent_engine.py`。
  2. 类 `SubagentRunner` → `SubagentEngine`（subagent_engine.py:56）；docstring/self 引用同步。
  3. 更新 import 点：`tests/test_delegate_tool.py`（`from lanscoder.agent.subagent import SubagentRunner` → `from lanscoder.agent.subagent_engine import SubagentEngine`，12 处 `SubagentRunner(` → `SubagentEngine(`）。
  4. `grep -rn "SubagentRunner" lanscoder/ tests/` 清零 agent 层具体类引用（叶子 Protocol 原名保留：`lanscoder/subagent/types.py`、`tools/delegate.py`、`subagent/__init__.py`）。
- [ ] **Step 4: 5a 断环 + 5b 去双树（`subagent_engine.py`）。**
  1. `__init__` 加 `child_runner_factory: Callable[..., SessionTurnRunner]`（必需 keyword-only）。
  2. 删除两个方法内 `from lanscoder.agent.loop import AgentLoop`（原 :176、:279）。模块级不再 import loop。
  3. 抽 `_run_child_loop`：
     ```python
     def _run_child_loop(self, child_session, prompt, tools, observer):
         runner = self.child_runner_factory(
             session=child_session, tools=tools, observer=observer,
             cancellation_token=current_cancellation_token(),   # 每次调用取当前 token，P2-9
         )
         try:
             result = asyncio.run(runner.run_user_turn(prompt))
             return result, runner
         except AgentCancelledError:
             raise
         except Exception:
             return None, runner
     ```
  4. `_make_progress_callback`（:471-495）语义迁入新方法 `_make_child_observer(progress_tracker) -> TurnObserver`：`on_progress` 分流——`current_job_id()` 非 None → 写 `background_manager.get(job_id).progress`；否则写 `progress_tracker.update(state)`；`usage_provider` 由 `_run_child_loop` 返回的 runner 提供（`lambda: runner.usage_summary()`）——因 `_run_child_loop` 先拿 runner 后构造 SubagentResult，把 usage 收割留在两条 run 路径（读 `runner.usage_summary()`）。
  5. `_run_inline`/`_run_isolated` 改为：构造 child_session → prompt → `tools = self._tools_for_role(role)`（inline）或 `self._worktree_child_tools(...)`（isolated）→ `observer = self._make_child_observer(progress_tracker)` → `result, runner = self._run_child_loop(child_session, prompt, tools, observer)` → 用 `result.response` + `runner.usage_summary()` 收割 SubagentResult（isolated 保留 diff/worktree 装饰与 `waiting_for_user_input` 分支）。删 run-tail 重复（原 :176-222/:279-358 的共用尾巴）。
- [ ] **Step 5: loop.py 完整注入（公共 API 重塑）。**
  1. 删 `tools`/`enable_delegate_tool`/`progress_callback`/`subagent_runner` 参数。
  2. 协作对象改为必需 keyword-only（无默认、无兜底构造）：`request_builder: RequestBuilder`、`guardrails: TurnGuardrails`、`observer: TurnObserver`、`mcp_activation: McpActivationTracker`、`tool_executor: ToolExecutor`。`background_manager`/`background_tool_names`/`guidance_provider`/`cancellation_token`/`stream_event_handler`/`tool_event_handler`/`context_builder`/`context_manager`/`limits`/`request_options`/`context_window` 保留可选默认。
  3. `foreground_subagent` → `foreground_progress()`：
     ```python
     def foreground_progress(self) -> dict[str, Any] | None:
         return self._observer.foreground_progress()
     ```
  4. 三个 sync facade（`_run_user_turn_sync`/`_run_nudge_turn_sync`/`_resume_with_user_input_sync`）**保留为薄 facade 不动**，各自加注释：`# 删改任一 facade 必须同步迁移全部约 120 个测试调用点`。
  5. `replace_cancellation_token`/`clear_stream_events`/`tool_executor` 属性保留（前两个 resume 路径，P1-3；后一个约 20 处测试直调）。
- [ ] **Step 6: `_builders.py` 上移组装根 `app/runtime.py`。**
  1. 把 `create_agent_loop`/`register_loop_tools` 从 `lanscoder/agent/_builders.py` 移到 `lanscoder/app/runtime.py`（模块级函数）；删除 `_builders.py`。`_create_loop` 调 `runtime.create_agent_loop`。
  2. `create_agent_loop` 构造顺序（全注入）：
     a. 注册 caller tools + background 控制工具：`register_loop_tools(session, caller_tools=kwargs.pop("tools", None), background_manager=background_manager, provider=provider, request_options=request_options)`（**不**传 subagent_runner，delegate 注册在 e 步完成）。
     b. 构造 `SubagentEngine`（12 依赖）：`store=session.store`、`provider`、`tools=session.tool_registry.tools()`、`project_root`、`agents_md=session.agents_md`、`skill_catalog=session.skill_catalog`、`permission_manager=session.permission_manager`、`sandbox_access=session.sandbox_access`（任务 7 换 coordinator）、`request_options`、`limits`、`background_manager`、`child_runner_factory=_child_runner_factory`。
     c. 构造 `TurnObserver(stream_event_handler=stream_event_handler, tool_event_handler=tool_event_handler, foreground_progress_provider=lambda: engine.foreground_progress)`（engine 已在上步绑定，lambda 惰性读属性）。
     d. 构造 RequestBuilder / TurnGuardrails / McpActivationTracker / ToolExecutor（依赖同上，全部注入）。
     e. 若 `enable_delegate_tool=True`：`register_loop_tools(...)` 二阶段补 delegate——直接 `if "delegate" not in session.tool_registry.names(): session.tool_registry.register(create_delegate_tool(engine, parent_session_id=session.session_id))`（或 `register_loop_tools` 支持传入 `subagent_runner=engine` 参数复用）。
     f. `AgentLoop(session=session, provider=provider, context_builder=..., context_manager=..., limits=..., request_options=..., context_window=..., request_builder=..., guardrails=..., observer=observer, mcp_activation=tracker, tool_executor=tool_executor, background_manager=..., background_tool_names=..., guidance_provider=..., cancellation_token=..., stream_event_handler=..., tool_event_handler=...)`。`kwargs.pop("tools", None)` 保证不回传 loop。
  3. child_runner_factory 闭包（组装根闭合 provider/limits/request_options/enable_delegate_tool=False/background_manager=None）：
     ```python
     def _child_runner_factory(*, session, tools, observer, cancellation_token):
         return create_agent_loop(
             session=session, provider=provider, tools=tools, observer=observer,
             cancellation_token=cancellation_token, background_manager=None,
             enable_delegate_tool=False,
         )
     ```
  4. `AgentChatRunner.foreground_subagent`（:410-416）改 `loop.foreground_progress()`。
  5. `runtime.py` 更新 import（`RequestBuilder`/`TurnGuardrails`/`TurnObserver`/`McpActivationTracker`/`ToolExecutor`/`SubagentEngine`/`create_delegate_tool`/`register_loop_tools`/`create_agent_loop`）。
- [ ] **Step 7: `make_loop()` fixture（`tests/conftest.py`）。**
  ```python
  import pytest
  from lanscoder.app.runtime import create_agent_loop

  @pytest.fixture
  def make_loop():
      def _make_loop(*, session, provider, **overrides):
          return create_agent_loop(session=session, provider=provider, **overrides)
      return _make_loop
  ```
  迁移全部直接 `AgentLoop(` 构造到 `make_loop(...)`：`tests/test_agent_context_loop.py`、`test_agent_e2e.py`、`test_agent_skill_flow.py`、`test_background_jobs.py`、`test_model_request_options.py`、`test_delegate_tool.py`、`test_session_resume_service.py`、`test_app_factory.py`、`test_cleanup_contracts.py`、`test_app_runtime.py`（共约 131 处）。逐文件：测试函数签名加 `make_loop` fixture 参数，`AgentLoop(...)` → `make_loop(...)`，跑该文件到绿；`from lanscoder.agent.loop import AgentLoop` 保留（类型注解/`isinstance`）。
- [ ] **Step 8: 契约测试（`tests/test_agent_contracts.py`）+ 层边界。**
  1. `test_run_user_turn_contract_signature`：`params == ["self", "content", "attachments", "streaming"]`。
  2. `test_session_turn_runner_protocol_contract`：`isinstance(loop, SessionTurnRunner)`；`inspect.signature(loop.run_user_turn).parameters["content"]` 存在。
  3. `test_sync_facade_contract_signature`：`inspect.signature(AgentLoop._run_user_turn_sync).parameters == ["self", "content", "attachments"]`；`_run_nudge_turn_sync` == ["self"]；`_resume_with_user_input_sync` == ["self", "request_id", "answer"]；三个返回注解均为 `AgentTurnResult`。
  4. `test_subagent_engine_no_module_loop_import`：`importlib.import_module("lanscoder.agent.subagent_engine")` 后断言 `"lanscoder.agent.loop" not in sys.modules`（fresh interpreter 子进程更稳，参照 test_layer_boundaries 现有手法）。
  5. `test_subagent_runner_protocol_contract`：构造 `SubagentEngine(...)` 断言 `isinstance(engine, SubagentRunner)`（叶子 Protocol，`@runtime_checkable`）且 `engine.foreground_progress` 属性可读。
  6. `test_make_loop_uses_production_constructor`：`make_loop(session=s, provider=p)` 返回 `AgentLoop` 且 `loop.request_builder`/`loop.guardrails`/`loop._observer`/`loop.tool_executor`/`loop._mcp_activation` 全为非 None 注入对象；`assert runtime._create_loop.__func__ 经 create_agent_loop 同函数`（`inspect.getmodule` 断言两者同源）。
  7. `tests/test_layer_boundaries.py` 扩展：`test_loop_does_not_import_subagent_module`（import `lanscoder.agent.loop` 后 `"lanscoder.agent.subagent_engine" not in sys.modules`）；`test_tools_import_does_not_pull_agent` 现有参数化保留。
- [ ] **Step 9: 验证 + 提交。**
  `python -m pytest tests/test_agent_contracts.py tests/test_layer_boundaries.py tests/test_delegate_tool.py tests/test_agent_context_loop.py tests/test_background_jobs.py tests/test_app_runtime.py -q`；`ruff` + `black --check`；全量 `python -m pytest -q`（1400/1/1）。
  `git add lanscoder/agent/ports.py lanscoder/subagent/types.py lanscoder/agent/subagent_engine.py lanscoder/agent/loop.py lanscoder/app/runtime.py lanscoder/agent/_builders.py tests/conftest.py tests/test_agent_contracts.py tests/test_layer_boundaries.py tests/test_delegate_tool.py tests/test_agent_context_loop.py tests/test_agent_e2e.py tests/test_agent_skill_flow.py tests/test_background_jobs.py tests/test_model_request_options.py tests/test_session_resume_service.py tests/test_app_factory.py tests/test_cleanup_contracts.py tests/test_app_runtime.py` + commit `refactor: rationalize subagent engine with assembly root injection and make_loop fixture`

---

### Task 6: 缝 3 — PermissionResumeHandler（三值判别式子流程）

**Files:**
- Create: `lanscoder/agent/permission_resume.py`（`ResumeOutcome`、`PendingStore`、`PermissionResumeHandler`）
- Modify: `lanscoder/agent/loop.py`（删 8 个 resume 方法；入口 glue 改调 handler；加 `permission_resume` 注入）
- Modify: `lanscoder/app/runtime.py`（`create_agent_loop` 构造 handler 注入 + 绑 `on_tool_round_completed` 回调）
- Modify: `tests/test_agent_contracts.py`（`test_resume_outcome_discriminant`）

**Interfaces:**
- Consumes: `PendingPermissionExecution`、`AgentTurnResult`/`AgentTurnStatus`、`ChatResponse`、`UserInputRequest`、`ToolResult`、`make_permission_denied_result`/`make_prewrite_review_failed_result`/`make_prewrite_review_stale_result`、`PermissionDecision`/`PermissionDecisionKind`、`ToolExecutor`、`TurnObserver`、`ChatProvider`、`anyio`。
- Produces（任务 7 依赖）:
  ```python
  @dataclass(frozen=True)
  class ResumeOutcome:
      kind: Literal["continue", "wait_for_input", "finished"]
      result: AgentTurnResult | None = None

  class PendingStore(Protocol):
      def get(self, request_id: str) -> PendingPermissionExecution | None: ...
      def clear(self) -> None: ...

  class PermissionResumeHandler:
      def __init__(self, *, pending_store: PendingStore, provider: ChatProvider, tool_executor: ToolExecutor,
                   observer: TurnObserver, session: AgentSession,
                   on_tool_round_completed: Callable[[], None]) -> None: ...
      async def handle(self, request_id: str, answer: str) -> ResumeOutcome: ...
  ```

**PendingStore 依赖契约（P1-4）**：pending 查找经最小 `PendingStore` 协议注入 handler；本任务实现为 session 的薄适配，任务 7 缝 4 只换实现（coordinator-backed）不改 handler 逻辑。

- [ ] **Step 1: 创建 `lanscoder/agent/permission_resume.py`。** 按上面接口平移 loop.py:451-609：
  - `handle` = `_append_permission_resume_result`（:451-469）判别式化：request 无匹配 → `finished`；链式 pending → `wait_for_input`；整批跑完 → `continue`。
  - `_pending_permission_for_resume`（:471-496）的 pending 查找改 `self._pending_store.get(request_id)`；`permission_manager is None` 检查读 `session.permission_manager`（任务 7 换 coordinator）。
  - `_prepare_permission_resume`（:498-528）/`_execute_resumed_permission_tool_call`（:530-532，调 `self._tool_executor.execute_after_permission_with_cancellation_context`）/`_emit_finished_permission_resume`（:534-544）/`_finish_permission_resume`（:546-559，`self._on_tool_round_completed()` 替换 `self._tool_rounds_completed += 1`；`session.append_tool_result`/`self._tool_executor.execute_interactive_async` 保留）/`_resolve_pending_confirmation`（:561-579）/`_blocked_permission_resume_result`（:581-609）平移。
  - `_emit_tool_event(...)` → `self._observer.on_tool_event(ToolExecutionEvent(...))`。
  - `_pending_turn_result(chained)` → handler 内联 `AgentTurnResult(status=AgentTurnStatus.WAITING_FOR_USER_INPUT, pending_input=chained)`。
- [ ] **Step 2: `lanscoder/agent/session.py` 加 PendingStore 薄适配。** 新增：
  ```python
  class SessionPendingStore:
      def __init__(self, session: AgentSession) -> None:
          self._session = session
      def get(self, request_id: str) -> PendingPermissionExecution | None:
          pending = self._session.pending_permission_execution
          if pending is not None and pending.request_id == request_id:
              return pending
          return None
      def clear(self) -> None:
          self._session.pending_permission_execution = None
  ```
- [ ] **Step 3: 修改 `lanscoder/agent/loop.py`。**
  1. 删除方法：`_append_permission_resume_result`（:451）、`_pending_permission_for_resume`（:471）、`_prepare_permission_resume`（:498）、`_execute_resumed_permission_tool_call`（:530）、`_emit_finished_permission_resume`（:534）、`_finish_permission_resume`（:546）、`_resolve_pending_confirmation`（:561）、`_blocked_permission_resume_result`（:581）。相应 import（`make_permission_denied_result`/`make_prewrite_review_failed_result`/`make_prewrite_review_stale_result`、`PermissionDecision`/`PermissionDecisionKind`）若不再被 loop 使用则移除。
  2. `__init__` 加 `permission_resume: PermissionResumeHandler`（必需 keyword-only，本任务起为注入）。
  3. `_resume_with_user_input_async`（:348-379）与 `_resume_with_user_input_streaming`（:381-398）的 glue 替换：
     ```python
     outcome = await self.permission_resume.handle(request_id, answer)
     if outcome.kind != "continue":
         return outcome.result   # finished / wait_for_input 均携带 AgentTurnResult
     self._begin_turn(new_user_turn=False)
     self._repair_interrupted_tool_calls_before_provider_request()
     self._check_cancelled()
     return await self._run_tool_loop(partial(self._complete_once_with_recovery, streaming=...))
     ```
     （超时/取消 pre-check 保留在 glue 最前，走 `self.guardrails.check_timeout()`/`self._check_cancelled()`。）
- [ ] **Step 4: `lanscoder/app/runtime.py` `create_agent_loop` 构造并注入 handler。** 构造 `PermissionResumeHandler(pending_store=SessionPendingStore(session), provider=provider, tool_executor=tool_executor, observer=observer, session=session, on_tool_round_completed=lambda: None)`；loop 构造完成后调 `handler.on_tool_round_completed = loop._record_resumed_tool_round`（loop 新增私有方法 `def _record_resumed_tool_round(self): self._tool_rounds_completed += 1`）——或 handler 提供 `set_tool_round_callback(cb)` 可写槽，组装根在 loop 返回后绑定。
- [ ] **Step 5: 契约测试 `test_resume_outcome_discriminant`（`tests/test_agent_contracts.py`）。** 三种 kind 各一用例：
  - 整批跑完 → `continue`（resume 后进入下一轮工具循环，最终 COMPLETED）；
  - 链式 pending → `wait_for_input`（结果 `status == WAITING_FOR_USER_INPUT`）；
  - request_id 不匹配 → `finished`（结果 `status == COMPLETED` 且 `finish_reason == "error"`）。
  驱动方式：直接构造 handler（`SessionPendingStore` + FakeProvider + 注入 tool_executor/observer）调用 `handle`，或经 `loop.resume_with_user_input`。
- [ ] **Step 6: 验证 + 提交。**
  `python -m pytest tests/test_agent_contracts.py tests/test_agent_context_loop.py tests/test_ask_user.py tests/test_delegate_tool.py -q`；`ruff` + `black --check`；全量 `python -m pytest -q`。
  `git add lanscoder/agent/permission_resume.py lanscoder/agent/session.py lanscoder/agent/loop.py lanscoder/app/runtime.py tests/test_agent_contracts.py` + commit `refactor: extract PermissionResumeHandler with tri-state ResumeOutcome`

---

### Task 7: 缝 4 — PermissionCoordinator + child 权限归口（5c）

**Files:**
- Create: `lanscoder/agent/permission.py`（`PermissionCoordinator`、`PreparedPermission`）
- Modify: `lanscoder/agent/session.py`（删 `set_permission_mode`/`_sync_sandbox_access_with_mode`；删 `permission_manager`/`sandbox_access`/`permission_policy` 状态字段，改持 `permission_coordinator` 引用 + `permission_mode` 只读访问器；create/from_project/resume 构造 coordinator）
- Modify: `lanscoder/agent/tool_execution.py`（权限逻辑全部改走 coordinator）
- Modify: `lanscoder/agent/permission_resume.py`（permission 读取改 coordinator；PendingStore 换 coordinator-backed 实现）
- Modify: `lanscoder/agent/subagent_engine.py`（5c：3 条 child 权限路径收敛）
- Modify: `lanscoder/app/runtime.py`（`create_agent_loop` 构造 coordinator 并注入 ToolExecutor/handler/SubagentEngine；`CurrentSessionState` 权限改走 coordinator）
- Modify: `tests/test_agent_contracts.py`（`test_permission_coordinator_call_order`）
- Modify: 受影响权限测试（13 处 `set_permission_mode` / 18 处 `session.mode` / 3 处 `sandbox_access` / 17 处 `PermissionMode.BYPASS`，迁移见 Step 6）

**Interfaces:**
- Consumes: `PermissionMode`/`PermissionRequest`/`PermissionDecision`/`PermissionDecisionKind`、`PermissionManager`、`SandboxAccess`、`ToolPermissionPreflight`、`PendingPermissionExecution`、`UserInputRequest`/`ToolResult`、`build_prewrite_review`/`supports_prewrite_review`、`make_permission_denied_result`/`make_prewrite_review_failed_result`。
- Produces:
  ```python
  @dataclass(slots=True)
  class PreparedPermission:
      result: ToolResult | None = None
      pending_input: UserInputRequest | None = None
      permission_request: PermissionRequest | None = None

  class PermissionCoordinator:
      def __init__(self, *, session: AgentSession, permission_manager: PermissionManager | None,
                   sandbox_access: SandboxAccess) -> None: ...
      def set_mode(self, mode: PermissionMode) -> PermissionMode
      @property
      def mode(self) -> PermissionMode
      @property
      def permission_manager(self) -> PermissionManager | None
      @property
      def sandbox_access(self) -> SandboxAccess
      @property
      def permission_policy(self) -> dict[str, object]
      def preflight(self, tool_call: ToolCall) -> ToolPermissionPreflight | None
      def prepare(self, tool_call: ToolCall, deferred_tool_calls: list[ToolCall]) -> PreparedPermission
      def store_pending_request(self, *, tool_call: ToolCall, request: PermissionRequest,
                                deferred_tool_calls: list[ToolCall], review_only: bool) -> UserInputRequest | ToolResult
      def requires_review(self, tool_call: ToolCall) -> bool
      def bypass_mutation(self, tool_call: ToolCall, *, preflight) -> ToolResult | None
      def child_permission_manager(self, *, root: str | None, mutation: bool, background: bool) -> PermissionManager | None
      def pending_get(self, request_id: str) -> PendingPermissionExecution | None
      def pending_clear(self) -> None
  ```

**行为不变式（spec §6.3）**：调用顺序 `preflight` →（`requires_review` 时）`store_pending_request` → `bypass_mutation`，由 `test_permission_coordinator_call_order` 钉死。5c 的三条路径 grant 差异逐一保留（spec §10 风险表）：inline 加 `NETWORK_REQUEST` grant、isolated 加 `WRITE_PATH`/`DELETE_PATH`（PATH_TREE 到 root）+ AGGRESSIVE、background-inline AGGRESSIVE 无 mutation grant。

- [ ] **Step 1: 创建 `lanscoder/agent/permission.py`。** `PreparedPermission` + `PermissionCoordinator`，平移：
  - `set_mode` body 自 session.py:516-524 + `_sync_sandbox_access_with_mode`（:526-543，改维护 `self._permission_policy` dict + `self._sandbox_access.mode`）。
  - `preflight` body 自 session.py:490-504（读 `session.tool_registry`，`isinstance(registry, PermissionAwareToolRegistry)`）。
  - `prepare` body 自 tool_execution._prepare_permission（:401-434）整体编排 + `_prepare_bypass_mutation`（:503-525）。
  - `store_pending_request` body 自 store_pending_permission_request（:557-607）+ store_pending_ask_user（:609-633）——写 `session.pending_permission_execution`/`session.persist_pending_permission_kind`。
  - `requires_review` 自 requires_prewrite_review（:489-491）+ `requires_bypass_prewrite_review`（:493-501，供 bypass_mutation）。
  - `pending_get`/`pending_clear`：读/清 `session.pending_permission_execution`。
  - `child_permission_manager(*, root, mutation, background)`：收敛 subagent_engine 三条路径（body 自 subagent.py:516-602），grant 差异见上。
- [ ] **Step 2: 修改 `lanscoder/agent/session.py`。**
  1. 删 `set_permission_mode`（:516）/`_sync_sandbox_access_with_mode`（:526）。
  2. 删字段 `permission_manager`（:104）/`permission_policy`（:105）/`sandbox_access`（:106）；加 `permission_coordinator: PermissionCoordinator | None = None`（dataclass 字段，`from __future__ import annotations` 下类型注解不构成运行期环）。加只读访问器 `permission_mode`（返回 `coordinator.mode.value`，coordinator 为 None 时返回 `"default"`）。
  3. `create`/`from_project`/`resume`（:117-265）：仍接收 `permission_manager`/`sandbox_access` 参数，构造 `coordinator = PermissionCoordinator(session=session, permission_manager=permission_manager, sandbox_access=sandbox_access or SandboxAccess())` 存 `session.permission_coordinator`；`mode` 字段初始化 = `permission_manager.mode.value if permission_manager else "default"`（保留 `self.mode` 字符串字段供 build_system_prefix——或改读 `permission_mode`）。
  4. `build_system_prefix`（:384-386）：`permission_policy=self.permission_policy` → `permission_policy=self.permission_coordinator.permission_policy if self.permission_coordinator else dict(DEFAULT_PERMISSION_POLICY)`。
  5. `restore_pending_permission_execution`（:283、:307-308）：`self.preflight_tool_call_permission(tool_call)` → `self.permission_coordinator.preflight(tool_call)`；`self.permission_manager.policy.project_root`/`self.sandbox_access` → `self.permission_coordinator.permission_manager.policy.project_root`/`self.permission_coordinator.sandbox_access`。
  6. 删 `preflight_tool_call_permission`（:490-504）方法本身（调用方改 coordinator）。
  7. `pending_permission_execution`/`pending_permission_input_request`（:334-355，经 `self.permission_manager` 读 mode）保留，内部改读 coordinator。
- [ ] **Step 3: 修改 `lanscoder/agent/tool_execution.py`。** `ToolExecutor.__init__` 加 `permission_coordinator: PermissionCoordinator`（必需）；`_prepare_permission`（:401）→ `self._permission_coordinator.prepare(tool_call, deferred_tool_calls)`；`_prepare_bypass_mutation`（:503）→ `self._permission_coordinator.bypass_mutation(...)`；`store_pending_permission_request`（:557）/`store_pending_ask_user`（:609）→ `self._permission_coordinator.store_pending_request(...)`；`preflight_tool_call_permission`（:408/:481）→ `self._permission_coordinator.preflight(...)`；`requires_prewrite_review`/`requires_bypass_prewrite_review`（:489-501）→ `self._permission_coordinator.requires_review(...)`/coordinator 内部；删除对 `self.session.permission_manager`/`self.session.sandbox_access`/`self.session.require_prewrite_review` 的直接读取（改 coordinator）。
- [ ] **Step 4: 修改 `lanscoder/agent/permission_resume.py`。** `_resolve_pending_confirmation`/`_blocked_permission_resume_result` 中 `session.permission_manager`/`session.preflight_tool_call_permission`/`session.sandbox_access` → `coordinator.permission_manager`/`coordinator.preflight`/`coordinator.sandbox_access`（handler 加 `permission_coordinator` 注入）；PendingStore 实现换 coordinator-backed：`pending_store = CoordinatorPendingStore(coordinator)`（`get` = `coordinator.pending_get`，`clear` = `coordinator.pending_clear`）——handler 逻辑零改动。
- [ ] **Step 5: 修改 `lanscoder/agent/subagent_engine.py`（5c）。** 删 `_child_permission_manager_for_inline`（:516）/`_child_permission_manager`（:543）/`_background_child_permission_manager`（:591），`create_child_session`（:383-391）/`_create_isolated_child_session`（:447-456）改调 `self.permission_coordinator.child_permission_manager(root=..., mutation=..., background=...)`（inline: `root=None, mutation=False, background=False`；isolated: `root=worktree.path, mutation=True`；background-inline: `root=self.project_root, mutation=False, background=True`）。`SubagentEngine.__init__` 加 `permission_coordinator` 依赖，替换 `permission_manager`/`sandbox_access`。
- [ ] **Step 6: `lanscoder/app/runtime.py` 组装根接线 + 测试迁移。**
  1. `create_agent_loop`：构造 `PermissionCoordinator(session=session, permission_manager=session.permission_manager, sandbox_access=session.sandbox_access)`——注意 session 已在 Step 2 删了这两个字段，故从 `session.permission_coordinator` 取用（session 已在 create 时构造 coordinator）；ToolExecutor/handler/SubagentEngine 装配改传 coordinator。
  2. `CurrentSessionState.set_permission_mode`（runtime.py:94-95）→ `self.session.permission_coordinator.set_mode(mode)`；`CurrentSessionState.mode`（:91-92）读 `self.session.permission_mode`。
  3. 测试迁移（机械）：
     - `session.set_permission_mode(...)` → `session.permission_coordinator.set_mode(...)`：`tests/test_app_runtime.py`、`tests/test_session_resume_service.py`、`tests/test_agent_context_loop.py`、`tests/test_cli.py`、`tests/test_permission_commands.py`（共 13 处）。
     - `session.mode` 读取 → `session.permission_mode`（18 处）；`session.permission_manager`/`session.sandbox_access`/`session.permission_policy` 读取 → `session.permission_coordinator.*`（3+ 处）；`PermissionMode.BYPASS` 用法（17 处）逐个核对仍走 `set_mode(BYPASS)` 语义（sandbox unrestricted + policy allow）。
     - 逐文件跑绿 `python -m pytest tests/test_permission_commands.py tests/test_permission_view.py tests/test_permissions_manager.py tests/test_prewrite_review.py tests/test_agent_context_loop.py tests/test_session_resume_service.py tests/test_cli.py -q`。
- [ ] **Step 7: 契约测试（`tests/test_agent_contracts.py`）。**
  1. `test_permission_coordinator_call_order`：驱动一次工具执行（模型返回需 review 的 write tool_call），用 RecordingCoordinator（包一层或 monkeypatch `prepare` 记录内部调用）断言顺序 `preflight` → `store_pending_request` → `bypass_mutation`（钉 §6.3 行为不变式）。
  2. `test_permission_coordinator_contract_signature`：`inspect.signature(PermissionCoordinator.set_mode/preflight/prepare/store_pending_request/requires_review/bypass_mutation)` 参数名逐一断言。
  3. `test_coordinator_pending_store_swap`：handler 经 `CoordinatorPendingStore` 走 `pending_get`/`pending_clear`，断言与 `SessionPendingStore` 行为等价（request_id 匹配/不匹配）。
- [ ] **Step 8: 验证 + 提交。**
  `ruff` + `black --check`；受影响权限测试 + 契约测试跑绿；全量 `python -m pytest -q`（预期 **1400 passed / 1 skipped / 1 failed**，唯一失败为既有 `test_mcp_integration.py` 环境问题，不修）。
  `git add lanscoder/agent/permission.py lanscoder/agent/session.py lanscoder/agent/tool_execution.py lanscoder/agent/permission_resume.py lanscoder/agent/subagent_engine.py lanscoder/app/runtime.py tests/test_agent_contracts.py tests/test_app_runtime.py tests/test_session_resume_service.py tests/test_agent_context_loop.py tests/test_cli.py tests/test_permission_commands.py` + commit `refactor: converge permission domain into PermissionCoordinator with child permission consolidation`

---

## 自审

- **Spec 覆盖**：§5 七个缝各对应一个任务（1/2/7/6/5/3/4），缝 5 的 5a/5b/5c/5d 全覆盖（5c 在任务 7）；§6.3 全部 11 个契约测试名落到对应缝任务（task1: 2 个追加签名/行为测试；task2: guardrails 签名/行为；task3: registry_complete_before_loop；task4: observer_consumer_pipeline / mcp_activation_tracker_turn_boundary / resume_rebind_observers / loop_tool_executor_attribute_contract；task5: run_user_turn_contract_signature / session_turn_runner_protocol_contract / sync_facade_contract_signature / subagent_engine_no_module_loop_import；task6: resume_outcome_discriminant；task7: permission_coordinator_call_order）；§9 顺序 1→2→7→6→5→3→4 逐字遵循；§8 非目标在 Global Constraints 复刻。
- **占位符扫描**：无 TBD/TODO/"适当处理"；每个代码步骤含实际方法名/签名/行号/断言意图。
- **类型一致**：`_complete_once(streaming=...)`、`_run_tool_loop(complete_once)`、`McpActivationTracker`、`SubagentEngine`、`ResumeOutcome(kind/result)`、`SessionTurnRunner`、`PermissionCoordinator`、`TurnObserver`、`ToolEventSink`、`PreparedPermission` 在任务间命名一致，无同名异义。
- **顺序依赖**：任务 1（loop 减负）先于任务 6/7；任务 3（registry 完备）先于任务 5（SubagentEngine 工具列表读 registry）；任务 4（observer/tracker）先于任务 5（完整注入依赖 observer）；任务 5（断环）先于任务 7（等环断再动权限最广面）；任务 6（handler 依赖减负后 loop）后于任务 1。任务间 Consumes/Produces 均衔接。
