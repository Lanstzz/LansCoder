# AgentLoop sync/async 双树统一实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `lanscoder/agent/loop.py` 的 sync/async 双树统一成单一 async 核心，删除同步树，非流式路径改走统一 async 核心。

**Architecture:** 保留"循环核心按 `complete_once` 注入"的既有模式，但注入的 `complete_once` 变为 async。provider 单步层合并成一个 `_complete_once(streaming=...)`，recovery 统一采用流式的丰富语义。公共 API（`run_user_turn` / `run_nudge_turn` / `resume_with_user_input`）签名不变。

**Tech Stack:** Python 3, anyio（`to_thread.run_sync` 跳线程）、pytest。

**Spec:** `docs/superpowers/specs/2026-08-20-agent-loop-sync-async-unification-design.md`

## Global Constraints

- 生产代码改动**仅限** `lanscoder/agent/loop.py`；`app/runtime.py`、`subagent.py`、`factory.py`、provider/tool_executor/session 零改动。
- 公共 API 签名不变（`streaming` 参数保留）。
- 工具执行必须保持走 `to_thread`（`execute_interactive_async`），保证 `SubagentRunner._run_inline` 的 `asyncio.run` 嵌套安全。
- recovery 统一用流式的丰富语义：retryable 失败重试一次 → 回退同步 complete；prompt-too-long → compact 重试。
- 每个任务结束仓库必须是绿的（相关测试全过）。
- 任务内跑 `ruff check` + `black --check`（仓库用 black，不用 ruff format）。
- 全量测试允许的唯一失败：`tests/test_mcp_integration.py`（缺 `mcp.server.fastmcp`，环境问题，与本次无关）。
- CLAUDE.md：未经用户要求不 commit。**本计划的 commit 步骤跳过，改为报告 git status。**

---

### Task 1: 统一 provider 单步层（重命名旧 sync 版腾名 + 新增统一版 + 新测试）

**Files:**
- Modify: `lanscoder/agent/loop.py`
- Test: `tests/test_model_request_options.py`

**Interfaces:**
- Consumes: 现有 `_prepare_main_provider_request` / `_reserve_provider_call` / `_check_turn_timeout` / `_check_cancelled` / `_record_projection_consumed` / `_report_progress` / `_compact_for_prompt_too_long`（全部保留不改）。
- Produces: `async _complete_once(*, tool_choice, runtime_instruction, streaming) -> ChatResponse`、`async _complete_once_with_recovery(*, tool_choice, runtime_instruction, streaming) -> ChatResponse`、`_complete_once_sync`、`_complete_once_sync_with_recovery`（供 Task 3 删除前同步树继续使用）。

- [ ] **Step 1: 重命名旧同步 provider 方法，腾出规范名**

在 `lanscoder/agent/loop.py`：
1. 同步 `def _complete_once(self, *, tool_choice="auto", runtime_instruction=None)`（约 623 行）改名 `def _complete_once_sync(...)`，**函数体不变**。
2. 同步 `def _complete_once_with_recovery(self, *, tool_choice="auto", runtime_instruction=None)`（约 655 行）改名 `def _complete_once_sync_with_recovery(...)`，并把它函数体内**两处** `self._complete_once(` 调用改为 `self._complete_once_sync(`（初始调用 + compact 后重试调用）。
3. 更新同步树的引用：`_run_user_turn_sync` 里的 `self._run_tool_loop_interactive(self._complete_once_with_recovery)` 改为 `self._run_tool_loop_interactive(self._complete_once_sync_with_recovery)`。
4. 更新测试 `tests/test_model_request_options.py:45`：`loop._complete_once()` → `loop._complete_once_sync()`。

验证：`python -m pytest tests/test_model_request_options.py -q` 全绿。

- [ ] **Step 2: 新增统一 async provider 单步层**

在 `lanscoder/agent/loop.py` 新增（放在 `_complete_once_sync` 附近）：

```python
    async def _complete_once(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
        streaming: bool,
    ) -> ChatResponse:
        """构造一次 provider 请求并获得模型响应（统一 sync/streaming）。

        streaming=True 时消费 provider.astream 并转发事件；streaming=False 时在
        worker 线程里跑 provider.complete，避免阻塞事件循环。
        """
        prepared = self._prepare_main_provider_request(
            tool_choice=tool_choice,
            runtime_instruction=runtime_instruction,
        )
        self._reserve_provider_call()
        self._check_turn_timeout()
        self._check_cancelled()
        if not streaming:
            response = await anyio.to_thread.run_sync(self.provider.complete, prepared.request)
        else:
            start_event_count = len(self.last_stream_events)
            final_response: ChatResponse | None = None
            try:
                async for event in self.provider.astream(prepared.request):
                    self._check_cancelled()
                    self.last_stream_events.append(event)
                    if self.stream_event_handler is not None:
                        self.stream_event_handler(event)
                    if event.kind == "message_completed":
                        final_response = event.response
            except ProviderError:
                # 失败的 streaming 尝试不把已收到的局部 delta 当真实回答留给 UI
                del self.last_stream_events[start_event_count:]
                raise
            if final_response is None:
                raise ProviderError(
                    ProviderErrorKind.API_ERROR,
                    "provider stream ended without message_completed event",
                )
            response = final_response
        self._record_projection_consumed(prepared)
        self._report_progress(response)
        return response

    async def _complete_once_with_recovery(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
        streaming: bool,
    ) -> ChatResponse:
        """统一 recovery：retryable 失败重试一次 → 回退同步 complete；prompt-too-long → compact 重试。"""
        retryable_failures = 0
        while True:
            try:
                return await self._complete_once(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                    streaming=streaming,
                )
            except ProviderError as exc:
                if exc.retryable:
                    if retryable_failures == 0:
                        retryable_failures += 1
                        continue
                    return await self._complete_once(
                        tool_choice=tool_choice,
                        runtime_instruction=runtime_instruction,
                        streaming=False,
                    )
                if not exc.requires_compaction:
                    raise
                result = self._compact_for_prompt_too_long(runtime_instruction=runtime_instruction)
                if result is None or result.status != "success":
                    raise
                return await self._complete_once(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                    streaming=streaming,
                )
```

- [ ] **Step 3: 写新增测试**

在 `tests/test_model_request_options.py` 追加（复用该文件现有 `RecordingProvider` 与 `_session(tmp_path)` 约定；新增一个 retryable fake）。文件顶部 import 区加 `from lanscoder.providers.errors import ProviderError, ProviderErrorKind`：

```python
class RetryableOnceProvider(ChatProvider):
    """complete 第一次抛 retryable 错误，之后成功；astream 委托给 base。"""

    def __init__(self, base: RecordingProvider) -> None:
        self._base = base
        self._failures = 0

    @property
    def name(self) -> str:
        return self._base.name

    @property
    def model(self) -> str:
        return self._base.model

    def complete(self, request: ChatRequest) -> ChatResponse:
        self._base.requests.append(request)
        if self._failures == 0:
            self._failures += 1
            raise ProviderError(ProviderErrorKind.API_ERROR, "boom", retryable=True)
        return ChatResponse(provider=self.name, model=self.model, content="ok")

    async def astream(self, request: ChatRequest):
        async for event in self._base.astream(request):
            yield event


def test_unified_complete_once_sync_mode_returns_provider_response(tmp_path) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = AgentLoop(session=session, provider=provider)
    session.append_user_message("hi")
    response = asyncio.run(loop._complete_once(streaming=False))
    assert response.content == "ok"


def test_unified_complete_once_streaming_mode_collects_events(tmp_path) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = AgentLoop(session=session, provider=provider)
    session.append_user_message("hi")
    response = asyncio.run(loop._complete_once(streaming=True))
    assert response.content == "ok"
    assert [e.kind for e in loop.last_stream_events] == ["message_completed"]


def test_unified_recovery_retries_retryable_error_once_for_sync_mode(tmp_path) -> None:
    # 关键新行为（spec 4.3）：非流式也获得 retryable 重试
    base = RecordingProvider()
    session = _session(tmp_path)
    loop = AgentLoop(session=session, provider=RetryableOnceProvider(base))
    session.append_user_message("hi")
    response = asyncio.run(loop._complete_once_with_recovery(streaming=False))
    assert response.content == "ok"
    assert loop.provider_call_count == 2
```

注意：`RecordingProvider.astream` 只产一个 `message_completed` 事件，所以 streaming 事件列表断言为 `["message_completed"]`，不要臆造 `message_started`。`provider_call_count` 由 `loop._reserve_provider_call()` 维护。

- [ ] **Step 4: 跑测试确认全绿**

Run: `python -m pytest tests/test_model_request_options.py -q`
Expected: PASS（新增 3 个 + 既有用例全过）

- [ ] **Step 5: 跑 lint**

Run: `ruff check lanscoder/agent/loop.py tests/test_model_request_options.py && black --check lanscoder/agent/loop.py tests/test_model_request_options.py`
Expected: 全部通过，无改动需求。

---

### Task 2: 把 async 树接线到统一 provider，删除旧 stream provider

**Files:**
- Modify: `lanscoder/agent/loop.py`
- Test: `tests/test_model_request_options.py`、`tests/test_agent_context_loop.py`

**Interfaces:**
- Consumes: Task 1 的 `_complete_once_with_recovery(streaming=...)`。
- Produces: streaming 树全部改走统一 provider；`_stream_once` / `_stream_once_with_recovery` / `_stream_once_attempt` 被删除。

- [ ] **Step 1: 把 streaming 入口的 provider 步骤切到统一版**

在 `lanscoder/agent/loop.py` 中，三处 streaming 入口把 `self._stream_once_with_recovery` 替换为 `functools.partial(self._complete_once_with_recovery, streaming=True)`：
- `_run_user_turn_streaming`（约 401 行）：`self._run_tool_loop_interactive_async(self._stream_once_with_recovery)` → `self._run_tool_loop_interactive_async(functools.partial(self._complete_once_with_recovery, streaming=True))`
- `_run_nudge_turn_streaming`（约 423 行）：同样替换
- `_resume_with_user_input_streaming`（约 373 行）：`self._run_tool_loop_interactive_async(self._stream_once_with_recovery)` → 同样替换

在文件顶部 import 区加 `from functools import partial`（若未导入；`loop.py` 当前没有 functools import，需新增）。若不想加 import，可用 lambda：`lambda *, tool_choice="auto", runtime_instruction=None: self._complete_once_with_recovery(tool_choice=tool_choice, runtime_instruction=runtime_instruction, streaming=True)`。**二选一，保持一致。**

- [ ] **Step 2: 删除旧 stream provider 三个方法**

删除 `lanscoder/agent/loop.py` 中的 `_stream_once`、`_stream_once_with_recovery`、`_stream_once_attempt` 三个方法（约 683-767 行）——Task 1 的统一版已覆盖其全部逻辑（含 streaming 事件收集、retryable 重试、失败裁剪局部事件）。

- [ ] **Step 3: 更新引用 `_stream_once` 的测试**

两处：
- `tests/test_model_request_options.py:81`：`asyncio.run(loop._stream_once())` → `asyncio.run(loop._complete_once(streaming=True))`
- `tests/test_agent_context_loop.py:940`：`asyncio.run(AgentLoop(session=session, provider=provider)._stream_once())` → `asyncio.run(AgentLoop(session=session, provider=provider)._complete_once(streaming=True))`

- [ ] **Step 4: 跑 streaming 相关测试**

Run: `python -m pytest tests/test_model_request_options.py tests/test_agent_context_loop.py -q`
Expected: PASS（streaming 树行为不变，由既有用例护栏覆盖）

- [ ] **Step 5: 跑 lint + git status**

Run: `ruff check lanscoder/agent/loop.py tests/test_model_request_options.py && black --check lanscoder/agent/loop.py tests/test_model_request_options.py`
Run: `git status --short`
Expected: 仅 `lanscoder/agent/loop.py`、`tests/test_model_request_options.py` 有改动。

---

### Task 3: 非流式走统一核心 + 删除同步树 + 迁移测试

**Files:**
- Modify: `lanscoder/agent/loop.py`
- Test: `tests/test_agent_e2e.py`、`tests/test_session_resume_service.py`、`tests/test_agent_skill_flow.py`、`tests/test_background_jobs.py`、`tests/test_model_request_options.py`

**Interfaces:**
- Consumes: Task 1-2 的统一 provider 层 + async 循环核心。
- Produces: 同步树全部删除；async 树方法去 `_async` 后缀成为规范名；~23 处测试调用迁移。

- [ ] **Step 1: 非流式入口改走统一 async 核心**

把三个 public 入口的非流式分支从"塞线程跑同步树"改为"直接跑 async 核心"：

`run_user_turn`（约 209-220 行）：
```python
    async def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
        streaming: bool = False,
    ) -> AgentTurnResult:
        if streaming:
            return await self._run_user_turn_streaming(content, attachments=attachments)
        return await self._run_user_turn_async(content, attachments=attachments)
```
新增 `_run_user_turn_async`（sync 树 `_run_user_turn_sync` 的 body 改造成 async 版）：
```python
    async def _run_user_turn_async(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )
        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self.session.append_user_message(content, attachments=attachments)
        return await self._run_tool_loop(
            functools.partial(self._complete_once_with_recovery, streaming=False)
        )
```

`run_nudge_turn`（约 222-233 行）：
```python
    async def run_nudge_turn(self, *, streaming: bool = False) -> AgentTurnResult:
        if streaming:
            return await self._run_nudge_turn_streaming()
        return await self._run_nudge_turn_async()
```
新增 `_run_nudge_turn_async`（把 sync 版 `_run_nudge_turn_sync` body 改 async）：
```python
    async def _run_nudge_turn_async(self) -> AgentTurnResult:
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )
        if self.background_manager is None or not self.background_manager.pending_completions(session_id=self.session.session_id):
            return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=None)
        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            functools.partial(self._complete_once_with_recovery, streaming=False)
        )
```

`resume_with_user_input`（约 308-319 行）：
```python
    async def resume_with_user_input(
        self,
        request_id: str,
        answer: str,
        *,
        streaming: bool = False,
    ) -> AgentTurnResult:
        if streaming:
            return await self._resume_with_user_input_streaming(request_id, answer)
        return await self._resume_with_user_input_async(request_id, answer)
```
新增 `_resume_with_user_input_async`（把 sync 版 body 改 async）：
```python
    async def _resume_with_user_input_async(self, request_id: str, answer: str) -> AgentTurnResult:
        try:
            self._check_turn_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())
        result = await self._append_permission_resume_result(request_id, answer)
        if result is not None:
            return result
        self._begin_turn(new_user_turn=False)
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            functools.partial(self._complete_once_with_recovery, streaming=False)
        )
```

- [ ] **Step 2: async 树方法去 `_async` 后缀成为规范名 + 合并重复的 sync 方法**

在 `lanscoder/agent/loop.py`：
1. `_append_permission_resume_result_async` → `_append_permission_resume_result`（删除 sync 版 `_append_permission_resume_result`）。body 用 async 版（sync 版里 `self._execute_resumed_permission_tool_call(pending)` 直接调用，async 版里是 `await anyio.to_thread.run_sync(self._execute_resumed_permission_tool_call, pending)`）。
2. `_finish_permission_resume_async` → `_finish_permission_resume`（删除 sync 版）。body 用 async 版（`await self.tool_executor.execute_interactive_async(...)`）。
3. `_run_tool_loop_interactive_async` → `_run_tool_loop`（删除 sync 版 `_run_tool_loop_interactive`）。
4. `_continue_tool_loop_from_response_async` → `_continue_tool_loop_from_response`（删除 sync 版）。
5. `_run_task_plan_reconciliation_if_needed_async` → `_run_task_plan_reconciliation_if_needed`（删除 sync 版）。
6. 把 `_run_user_turn_sync`、`_run_nudge_turn_sync`、`_resume_with_user_input_sync` 改成**薄 sync facade**（controller 已裁决，见 ledger）：
   ```python
   def _run_user_turn_sync(self, content: str, *, attachments: list[UserAttachment] | None = None) -> AgentTurnResult:
       return asyncio.run(self._run_user_turn_async(content, attachments=attachments))

   def _run_nudge_turn_sync(self) -> AgentTurnResult:
       return asyncio.run(self._run_nudge_turn_async())

   def _resume_with_user_input_sync(self, request_id: str, answer: str) -> AgentTurnResult:
       return asyncio.run(self._resume_with_user_input_async(request_id, answer))
   ```
   这三个方法只被测试直接调用（约 120 处，几乎全在同步测试里），保留薄 facade 避免大规模测试迁移；它们内部转调 Step 1 新增的 async 版本，不包含任何重复逻辑。`loop.py` 需确保 `import asyncio`（当前未导入，需新增）。
7. 删除 `_complete_once_sync`、`_complete_once_sync_with_recovery`（Task 1 腾出的旧 sync provider，现在无引用）。

删除后全仓库 grep `_run_user_turn_sync\|_run_nudge_turn_sync\|_resume_with_user_input_sync\|_complete_once_sync\|_stream_once` 应只剩注释/测试待迁移（Step 3 处理）。

- [ ] **Step 3: 迁移引用待删 provider 方法的测试（~5 处）**

`_run_user_turn_sync` / `_resume_with_user_input_sync` / `_run_nudge_turn_sync` 保留为薄 facade（Step 2 第 6 点），其 ~120 处测试调用无需迁移。只有引用将被删除的 `_complete_once_sync` / `_complete_once_sync_with_recovery` 的测试需要迁移（`_complete_once` 的 `streaming` 是 keyword-only 参数）：

- `tests/test_model_request_options.py:60`：`loop._complete_once_sync()` → `asyncio.run(loop._complete_once(streaming=False))`
- `tests/test_agent_context_loop.py:1719`：`loop._complete_once_sync(tool_choice="none")` → `asyncio.run(loop._complete_once(streaming=False, tool_choice="none"))`
- `tests/test_agent_context_loop.py:1780-1781`：`loop._complete_once_sync(runtime_instruction=...)` / `loop._complete_once_sync()` → `asyncio.run(loop._complete_once(streaming=False, runtime_instruction=...))` / `asyncio.run(loop._complete_once(streaming=False))`
- `tests/test_agent_context_loop.py:1804`：`loop._complete_once_sync_with_recovery(runtime_instruction=...)` → `asyncio.run(loop._complete_once_with_recovery(streaming=False, runtime_instruction=...))`
- `tests/test_agent_context_loop.py:2742`：`AgentLoop(...)._complete_once_sync()` → `asyncio.run(AgentLoop(...)._complete_once(streaming=False))`

迁移后 `grep -rn "_complete_once_sync" lanscoder tests --include="*.py"` 应为 0（除注释）。

- [ ] **Step 4: 删除重构后过期/失效的注释**

用户明确要求：重构后过期的注释一并删除。具体：

1. 随被删方法一起删除的：`_run_user_turn_sync`（243 行）等同步方法的章节头注释与 docstring，随代码删除自然消失。
2. 已失效的行号引用：`# 关键顺序（L745 附近）：...`（约 896 行）——行号早已偏移（本次及此前清理都动过行数），把 `（L745 附近）` 去掉，只保留语义描述：`# 关键顺序：必须先写 assistant tool_call，再写对应 tool_result。`
3. 重构后过时的结构描述：`_complete_once` 章节头（约 608 行）若仍写"同步调用、streaming 调用"的分工，更新为统一版的描述；模块头部"阅读路径导航"（1-21 行）若提及旧结构则同步更新，若仍准确则保留。
4. 全文扫描：`grep -n "L[0-9]\{2,4\} 附近\|_run_user_turn_sync\|_stream_once\|_complete_once_sync" lanscoder/agent/loop.py`，删除任何引用已删方法或失效行号的残留注释。

- [ ] **Step 5: 全量测试**

Run: `python -m pytest -q`
Expected: 1396 passed, 1 skipped, 1 failed（唯一失败为既有的 `test_mcp_integration.py` 环境问题）。

- [ ] **Step 6: 确认同步树零残留 + lint**

Run: `grep -rn "_run_user_turn_sync\|_run_nudge_turn_sync\|_resume_with_user_input_sync\|_complete_once_sync\|_stream_once\|_tool_loop_interactive\b" lanscoder tests --include="*.py" | grep -v __pycache__`
Expected: 无引用（除 `_run_tool_loop` 本身无此词）。

Run: `ruff check lanscoder/agent/loop.py && black --check lanscoder/agent/loop.py`
Run: `git status --short`
Expected: 生产改动仅 `lanscoder/agent/loop.py`；测试改动为上述 5 个文件。

---

## Self-Review

**Spec coverage:**
- §3.1 目标结构 → Task 1（provider 层）、Task 2（streaming 接线）、Task 3（非流式 + 改名）。
- §3.2 删除清单 11 个 sync 方法 → Task 1（rename 2 个 sync provider 腾名）、Task 2（删 3 个 stream provider）、Task 3（删剩余 6 个 sync 方法 + 2 个 rename 的 sync provider）。
- §3.4 provider 单步细节 → Task 1 Step 2。
- §3.5 统一 recovery → Task 1 Step 2。
- §3.6 行为变化（线程模型、非流式 retryable 增强）→ Task 1 Step 3 新测试覆盖；Task 3 非流式入口接线。
- §3.7 asyncio.run 嵌套安全 → Global Constraints 中"工具执行走 to_thread"约束 + Task 3 测试护栏。
- §4.2 测试迁移 5 文件 ~23 处 → Task 3 Step 3。
- §4.3 新增非流式 retryable 用例 → Task 1 Step 3。

**Placeholder scan:** 无 TBD/TODO；每个代码步骤含完整代码或精确行号/模式。

**Type consistency:** `_complete_once_with_recovery(streaming=...)` 在 Task 1 定义、Task 2/3 通过 `functools.partial(..., streaming=True/False)` 注入；`_run_tool_loop(complete_once)` 命名在 Task 3 Step 2 从 `_run_tool_loop_interactive_async` 统一改名并全文件一致；`_append_permission_resume_result` / `_finish_permission_resume` async 版成为唯一版本，入口调用一致。
