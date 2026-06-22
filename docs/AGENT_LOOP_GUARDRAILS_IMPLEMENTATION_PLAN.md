# Agent Loop 护栏实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务执行。每个任务都用 checkbox (`- [ ]`) 跟踪。每个阶段必须先写失败测试，再实现，通过聚焦测试后开子代理审阅；审阅通过后本地提交，再进入下一阶段。

**Goal:** 完整覆盖 `docs/AGENT_LOOP_GUARDRAILS_GOAL.md`：让 FirstCoder 的 agent loop 不再依赖很小的 `max_tool_rounds` 作为主刹车，而是具备成功验证收工、provider 调用上限、单轮总耗时上限、可配置预算和 benchmark 友好的默认值。

**Architecture:** 新增轻量策略层：`verification.py` 负责识别验证命令和成功验证结果，`loop_limits.py` 负责 loop 预算和停止原因。`AgentLoop` 接收 limits，统计 provider 调用次数和耗时，在成功验证后执行一次 `tool_choice="none"` 的最终模型调用。TUI runner 和后续 SWE-bench adapter 通过配置选择不同预算，不把 benchmark 专用逻辑写死进通用 loop。

**Tech Stack:** Python 3.12、现有 `AgentLoop` / `AgentSession` / `ChatRequest` / `ToolResult`、`pytest`、标准库 `time`、现有 fake provider 测试模式。

---

## 覆盖范围

本计划覆盖目标计划中的全部第一阶段目标：

- 成功验证后收工：识别 `shell` / `diagnostics` 中的验证命令成功，并强制下一次 provider 请求 `tool_choice="none"`。
- Provider 调用次数上限：一次用户 turn 内统计 provider 调用，超限后清晰停止。
- 单轮总耗时上限：一次用户 turn 超过配置时长后清晰停止。
- 可配置工具轮数上限：保留 `max_tool_rounds`，支持 `None` 表示不以工具轮数硬停。
- 工具超时策略：不重写全部工具，只把 loop 预算与工具 timeout 策略在文档和配置入口中打通。
- TUI / benchmark 不同默认值：普通 TUI 保守，SWE-bench Lite 计划中的 adapter 使用更大预算。

## 文件结构

- Create `firstcoder/agent/verification.py`
  - 识别验证命令。
  - 识别成功验证工具结果。

- Create `firstcoder/agent/loop_limits.py`
  - 定义 `AgentLoopLimits`。
  - 定义 loop 停止 finish reason 常量。

- Modify `firstcoder/agent/loop.py`
  - 接收 `limits`，并兼容旧的 `max_tool_rounds` 参数。
  - `_complete_once()` 和 streaming 路径支持 `tool_choice`。
  - 统计 provider 调用次数。
  - 检查单轮耗时。
  - 工具执行后检测成功验证，并触发 final-only provider call。
  - 保持权限确认和 tool_call/tool_result 顺序不变。

- Modify `firstcoder/app/runtime.py`
  - `AgentChatRunner` 接收并传递 `AgentLoopLimits`。
  - 保持已有 `max_tool_rounds` 兼容。

- Modify `firstcoder/app/factory.py`
  - 设置普通 TUI 的 loop 默认预算。

- Modify future eval implementation docs
  - Update `docs/SWE_LITE_IMPLEMENTATION_PLAN.md` 中 adapter 的 loop 预算建议。

- Test `tests/test_agent_verification.py`
  - 验证命令识别。
  - 成功/失败工具结果识别。

- Test `tests/test_agent_loop_limits.py`
  - limits 默认值和兼容构造。

- Modify `tests/test_agent_context_loop.py`
  - final-only 收束测试。
  - provider 调用上限测试。
  - 单轮超时测试。
  - `max_tool_rounds=None` 不触发工具轮数上限测试。

- Modify `tests/test_app_runtime.py`
  - runner 能传递 limits。

---

### Task 1: 验证命令识别

**Files:**
- Create: `firstcoder/agent/verification.py`
- Test: `tests/test_agent_verification.py`

- [x] **Step 1: 写失败测试**

Create `tests/test_agent_verification.py`:

```python
from firstcoder.agent.verification import (
    is_successful_verification_result,
    is_verification_command,
)
from firstcoder.tools.types import ToolResult


def test_is_verification_command_accepts_common_test_commands() -> None:
    assert is_verification_command("pytest -q")
    assert is_verification_command("python -m pytest tests/test_api.py -q")
    assert is_verification_command("/usr/bin/python3 -m pytest -q")
    assert is_verification_command("npm test")
    assert is_verification_command("pnpm test -- --runInBand")
    assert is_verification_command("yarn test")
    assert is_verification_command("go test ./...")
    assert is_verification_command("cargo test")


def test_is_verification_command_rejects_non_test_commands() -> None:
    assert not is_verification_command("python script.py")
    assert not is_verification_command("pytest-output-viewer")
    assert not is_verification_command("echo pytest")
    assert not is_verification_command("git diff")
    assert not is_verification_command("")


def test_successful_shell_verification_result() -> None:
    result = ToolResult(
        name="shell",
        ok=True,
        content="3 passed",
        data={"command": "python -m pytest -q", "exit_code": 0},
    )

    assert is_successful_verification_result("shell", result)


def test_successful_diagnostics_verification_result() -> None:
    result = ToolResult(
        name="diagnostics",
        ok=True,
        content="3 passed",
        data={"command": "pytest -q", "exit_code": 0},
    )

    assert is_successful_verification_result("diagnostics", result)


def test_failed_verification_result_does_not_count() -> None:
    result = ToolResult(
        name="shell",
        ok=False,
        content="1 failed",
        data={"command": "pytest -q", "exit_code": 1},
        error="命令退出码为 1",
    )

    assert not is_successful_verification_result("shell", result)


def test_non_verification_success_result_does_not_count() -> None:
    result = ToolResult(
        name="shell",
        ok=True,
        content="diff --git ...",
        data={"command": "git diff", "exit_code": 0},
    )

    assert not is_successful_verification_result("shell", result)
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_agent_verification.py -q
```

Expected: FAIL，因为 `firstcoder.agent.verification` 还不存在。

- [x] **Step 3: 最小实现**

Create `firstcoder/agent/verification.py`:

```python
"""Verification command detection for agent-loop guardrails."""

from __future__ import annotations

import shlex

from firstcoder.tools.types import ToolResult


_PACKAGE_TEST_COMMANDS = {
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("go", "test"),
    ("cargo", "test"),
}


def is_verification_command(command: str) -> bool:
    """Return True when a shell command looks like a project verification command."""

    stripped = command.strip()
    if not stripped:
        return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return False
    if not tokens:
        return False

    executable = _basename(tokens[0])
    if executable == "pytest":
        return True
    if executable.startswith("python") and len(tokens) >= 3 and tokens[1:3] == ["-m", "pytest"]:
        return True
    if len(tokens) >= 2 and (_basename(tokens[0]), tokens[1]) in _PACKAGE_TEST_COMMANDS:
        return True
    return False


def is_successful_verification_result(tool_name: str, result: ToolResult) -> bool:
    """Return True when a tool result proves that a verification command passed."""

    if tool_name not in {"shell", "diagnostics"}:
        return False
    if not result.ok:
        return False
    if result.data.get("exit_code") != 0:
        return False
    command = result.data.get("command")
    if not isinstance(command, str):
        return False
    return is_verification_command(command)


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]
```

- [x] **Step 4: 运行聚焦测试**

Run:

```bash
pytest tests/test_agent_verification.py -q
```

Expected: PASS.

- [x] **Step 5: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 1 of Agent Loop Guardrails. Focus on verification command detection correctness, avoiding false positives like "echo pytest", and whether ToolResult checks are conservative enough.
```

If review passes:

```bash
git add firstcoder/agent/verification.py tests/test_agent_verification.py
git commit -m "feat(agent): detect successful verification commands"
```

---

### Task 2: Loop Limits 数据结构

**Files:**
- Create: `firstcoder/agent/loop_limits.py`
- Test: `tests/test_agent_loop_limits.py`

- [x] **Step 1: 写失败测试**

Create `tests/test_agent_loop_limits.py`:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason


def test_default_limits_match_tui_goal_profile() -> None:
    limits = AgentLoopLimits.default()

    assert limits.max_tool_rounds == 20
    assert limits.max_provider_calls == 40
    assert limits.max_turn_seconds == 600
    assert limits.successful_verification_stop is True


def test_swe_lite_limits_match_goal_profile() -> None:
    limits = AgentLoopLimits.swe_lite()

    assert limits.max_tool_rounds == 60
    assert limits.max_provider_calls == 100
    assert limits.max_turn_seconds == 1800
    assert limits.successful_verification_stop is True


def test_summary_limits_disable_tool_loops() -> None:
    limits = AgentLoopLimits.summary()

    assert limits.max_tool_rounds == 1
    assert limits.max_provider_calls == 3
    assert limits.max_turn_seconds == 120


def test_legacy_max_tool_rounds_override() -> None:
    limits = AgentLoopLimits.default().with_max_tool_rounds(4)

    assert limits.max_tool_rounds == 4


def test_stop_reason_values_are_finish_reasons() -> None:
    assert AgentLoopStopReason.PROVIDER_CALL_LIMIT.value == "provider_call_limit"
    assert AgentLoopStopReason.TURN_TIMEOUT.value == "turn_timeout"
    assert AgentLoopStopReason.TOOL_ROUND_LIMIT.value == "tool_round_limit"
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_agent_loop_limits.py -q
```

Expected: FAIL，因为 `loop_limits.py` 还不存在。

- [x] **Step 3: 最小实现**

Create `firstcoder/agent/loop_limits.py`:

```python
"""Agent loop budget and stop-reason types."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class AgentLoopStopReason(StrEnum):
    TOOL_ROUND_LIMIT = "tool_round_limit"
    PROVIDER_CALL_LIMIT = "provider_call_limit"
    TURN_TIMEOUT = "turn_timeout"


@dataclass(frozen=True, slots=True)
class AgentLoopLimits:
    """Configurable guardrails for one user turn."""

    max_tool_rounds: int | None = 20
    max_provider_calls: int | None = 40
    max_turn_seconds: float | None = 600
    successful_verification_stop: bool = True

    @classmethod
    def default(cls) -> "AgentLoopLimits":
        return cls()

    @classmethod
    def swe_lite(cls) -> "AgentLoopLimits":
        return cls(
            max_tool_rounds=60,
            max_provider_calls=100,
            max_turn_seconds=1800,
            successful_verification_stop=True,
        )

    @classmethod
    def summary(cls) -> "AgentLoopLimits":
        return cls(
            max_tool_rounds=1,
            max_provider_calls=3,
            max_turn_seconds=120,
            successful_verification_stop=False,
        )

    def with_max_tool_rounds(self, value: int | None) -> "AgentLoopLimits":
        return replace(self, max_tool_rounds=value)
```

- [x] **Step 4: 运行聚焦测试**

Run:

```bash
pytest tests/test_agent_loop_limits.py -q
```

Expected: PASS.

- [x] **Step 5: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 2 of Agent Loop Guardrails. Focus on whether AgentLoopLimits covers the goal document, keeps defaults conservative for TUI, and provides a clear SWE Lite profile.
```

If review passes:

```bash
git add firstcoder/agent/loop_limits.py tests/test_agent_loop_limits.py
git commit -m "feat(agent): add loop guardrail limits"
```

---

### Task 3: AgentLoop 支持 tool_choice 和 limits 兼容

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [x] **Step 1: 写失败测试**

Append to `tests/test_agent_context_loop.py`:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits


def test_agent_loop_passes_tool_choice_none_for_final_only_completion(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_tool_choice", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="final")])
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits.default(),
    )

    response = loop._complete_once(tool_choice="none")

    assert response.content == "final"
    assert provider.requests[0].tool_choice == "none"
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_passes_tool_choice_none_for_final_only_completion -q
```

Expected: FAIL，因为 `AgentLoop.__init__` 还不接收 `limits`，`_complete_once()` 也不接收 `tool_choice`。

- [x] **Step 3: 修改 `AgentLoop.__init__`**

Modify imports in `firstcoder/agent/loop.py`:

```python
import time

from firstcoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason
```

Modify constructor signature:

```python
        max_tool_rounds: int | None = None,
        limits: AgentLoopLimits | None = None,
        clock=time.monotonic,
```

Replace max tool rounds assignment:

```python
        resolved_limits = limits or AgentLoopLimits.default()
        if max_tool_rounds is not None:
            resolved_limits = resolved_limits.with_max_tool_rounds(max_tool_rounds)
        self.limits = resolved_limits
        self.max_tool_rounds = resolved_limits.max_tool_rounds
        self.clock = clock
        self.provider_call_count = 0
        self.turn_started_at: float | None = None
```

- [x] **Step 4: 修改 `_complete_once()` 和 streaming 路径**

Change:

```python
    def _complete_once(self) -> ChatResponse:
```

to:

```python
    def _complete_once(self, *, tool_choice="auto") -> ChatResponse:
```

Before provider call:

```python
        self._check_provider_call_limit()
        self._check_turn_timeout()
        self.provider_call_count += 1
```

Change provider call:

```python
        return self.provider.complete(ChatRequest(messages=messages, tools=definitions, tool_choice=tool_choice))
```

Change `_stream_once()` similarly:

```python
    async def _stream_once(self, *, tool_choice="auto") -> ChatResponse:
```

and:

```python
        self._check_provider_call_limit()
        self._check_turn_timeout()
        self.provider_call_count += 1
        ...
        async for event in self.provider.astream(ChatRequest(messages=messages, tools=definitions, tool_choice=tool_choice)):
```

Change recovery wrappers to accept and pass `tool_choice`:

```python
    def _complete_once_with_recovery(self, *, tool_choice="auto") -> ChatResponse:
        ...
            return self._complete_once(tool_choice=tool_choice)
```

```python
    async def _stream_once_with_recovery(self, *, tool_choice="auto") -> ChatResponse:
        ...
            return await self._stream_once_attempt(tool_choice=tool_choice)
```

```python
    async def _stream_once_attempt(self, *, tool_choice="auto") -> ChatResponse:
        ...
            return await self._stream_once(tool_choice=tool_choice)
```

- [x] **Step 5: 添加 provider call / timeout helper**

Add methods to `AgentLoop`:

```python
    def _begin_turn(self) -> None:
        self.provider_call_count = 0
        self.turn_started_at = self.clock()

    def _check_provider_call_limit(self) -> None:
        limit = self.limits.max_provider_calls
        if limit is not None and self.provider_call_count >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.PROVIDER_CALL_LIMIT)

    def _check_turn_timeout(self) -> None:
        limit = self.limits.max_turn_seconds
        if limit is None or self.turn_started_at is None:
            return
        if self.clock() - self.turn_started_at >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.TURN_TIMEOUT)
```

Add private exception near `_ToolExecutionState`:

```python
class _AgentLoopLimitReached(Exception):
    def __init__(self, reason: AgentLoopStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
```

Call `_begin_turn()` at the start of `run_user_turn_interactive()`, `resume_with_user_input()`, `run_user_turn_streaming()`, and `resume_with_user_input_streaming()` before entering the loop. Do not call it when immediately returning pending permission input.

- [x] **Step 6: Run focused test**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_passes_tool_choice_none_for_final_only_completion -q
```

Expected: PASS.

- [x] **Step 7: Run existing loop tests**

Run:

```bash
pytest tests/test_agent_context_loop.py -q
```

Expected: PASS.

- [x] **Step 8: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 3 of Agent Loop Guardrails. Focus on backward compatibility for max_tool_rounds, correct ChatRequest.tool_choice propagation, and whether provider_call_count is reset per user turn.
```

If review passes:

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "feat(agent): pass loop limits and tool choice"
```

---

### Task 4: 成功验证后强制最终回答

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [x] **Step 1: 写失败测试**

Append to `tests/test_agent_context_loop.py`:

```python
def _success_tool() -> Tool:
    definition = ToolDefinition(
        name="shell",
        description="fake shell",
        parameters={"type": "object", "properties": {}},
    )

    def execute(**kwargs):
        return ToolResult(
            name="shell",
            ok=True,
            content="3 passed",
            data={"command": "pytest -q", "exit_code": 0, "stdout": "3 passed", "stderr": ""},
        )

    return Tool(definition=definition, executor=execute)


def test_agent_loop_forces_final_answer_after_successful_verification(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_verify_stop", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_test", name="shell", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="Tests pass."),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_success_tool()],
        limits=AgentLoopLimits.default(),
    ).run_user_turn("修测试")

    assert response.content == "Tests pass."
    assert len(provider.requests) == 2
    assert provider.requests[0].tool_choice == "auto"
    assert provider.requests[1].tool_choice == "none"
    assert [message.role for message in store.rebuild_session_view("sess_verify_stop").messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_forces_final_answer_after_successful_verification -q
```

Expected: FAIL，因为成功验证后还不会强制 `tool_choice="none"`。

- [x] **Step 3: 修改同步工具循环**

Import:

```python
from firstcoder.agent.verification import is_successful_verification_result
```

Add field to `_ToolExecutionState`:

```python
        successful_verification: bool = False,
```

and:

```python
        self.successful_verification = successful_verification
```

In `_execute_tool_calls_interactive()`, initialize:

```python
        successful_verification = False
```

After appending each tool result:

```python
            if is_successful_verification_result(tool_call.name, result):
                successful_verification = True
```

Return:

```python
        return _ToolExecutionState(
            task_hash_changed=task_hash_changed,
            successful_verification=successful_verification,
        )
```

In `_run_tool_loop_interactive()`, after compact calls and before incrementing/continuing:

```python
            if self.limits.successful_verification_stop and execution.successful_verification:
                response = self._drop_unsupported_tool_calls(complete_once(tool_choice="none"))
                break
```

Ensure the final `self.session.append_assistant_response(response)` remains unchanged.

- [x] **Step 4: 修改 async streaming 工具循环**

In `_run_tool_loop_interactive_async()`, mirror the same logic:

```python
            if self.limits.successful_verification_stop and execution.successful_verification:
                response = self._drop_unsupported_tool_calls(await complete_once(tool_choice="none"))
                break
```

- [x] **Step 5: Run focused test**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_forces_final_answer_after_successful_verification -q
```

Expected: PASS.

- [x] **Step 6: 写失败验证不会收工的测试**

Append:

```python
def _failed_test_tool() -> Tool:
    definition = ToolDefinition(
        name="shell",
        description="fake shell",
        parameters={"type": "object", "properties": {}},
    )

    def execute(**kwargs):
        return ToolResult(
            name="shell",
            ok=False,
            content="1 failed",
            data={"command": "pytest -q", "exit_code": 1, "stdout": "", "stderr": "1 failed"},
            error="命令退出码为 1",
        )

    return Tool(definition=definition, executor=execute)


def test_agent_loop_does_not_force_final_answer_after_failed_verification(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_verify_fail", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_test", name="shell", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="继续修复"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_failed_test_tool()],
        limits=AgentLoopLimits.default(),
    ).run_user_turn("修测试")

    assert response.content == "继续修复"
    assert provider.requests[1].tool_choice == "auto"
```

- [x] **Step 7: Run verification stop tests**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_forces_final_answer_after_successful_verification tests/test_agent_context_loop.py::test_agent_loop_does_not_force_final_answer_after_failed_verification -q
```

Expected: PASS.

- [x] **Step 8: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 4 of Agent Loop Guardrails. Focus on whether successful verification stop preserves legal tool_call/tool_result ordering, whether failed verification continues normally, and whether sync/async paths stay equivalent.
```

If review passes:

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "feat(agent): stop after successful verification"
```

---

### Task 5: Provider 调用次数上限

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [x] **Step 1: 写失败测试**

Append:

```python
def test_agent_loop_stops_when_provider_call_limit_is_reached(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_provider_limit", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "one"})],
                finish_reason="tool_calls",
            ),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        limits=AgentLoopLimits(
            max_tool_rounds=None,
            max_provider_calls=1,
            max_turn_seconds=None,
            successful_verification_stop=False,
        ),
    ).run_user_turn("调用工具")

    assert response.finish_reason == "provider_call_limit"
    assert "provider 调用次数达到上限" in response.content
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_stops_when_provider_call_limit_is_reached -q
```

Expected: FAIL，因为 `_AgentLoopLimitReached` 还没有被 loop 捕获并转换成 `ChatResponse`。

- [x] **Step 3: 添加 limit response helper**

In `firstcoder/agent/loop.py`, add:

```python
    def _limit_response(self, reason: AgentLoopStopReason) -> ChatResponse:
        messages = {
            AgentLoopStopReason.PROVIDER_CALL_LIMIT: (
                f"provider 调用次数达到上限（max_provider_calls={self.limits.max_provider_calls}），已停止继续执行。"
            ),
            AgentLoopStopReason.TURN_TIMEOUT: (
                f"本轮任务耗时达到上限（max_turn_seconds={self.limits.max_turn_seconds}），已停止继续执行。"
            ),
            AgentLoopStopReason.TOOL_ROUND_LIMIT: (
                f"工具调用轮次达到上限（max_tool_rounds={self.limits.max_tool_rounds}），已停止继续执行工具。"
            ),
        }
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=messages[reason],
            finish_reason=reason.value,
        )
```

Change `_tool_round_limit_response()` to use `_limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT)`, while preserving provider/model from current provider. If tests require raw response preservation, keep existing raw behavior by adding optional `raw` parameter.

- [x] **Step 4: 捕获 `_AgentLoopLimitReached`**

Wrap `_run_tool_loop_interactive()` body:

```python
        try:
            response = self._drop_unsupported_tool_calls(complete_once())
            ...
        except _AgentLoopLimitReached as exc:
            response = self._limit_response(exc.reason)
```

Ensure after catching, the final assistant response is appended once and the method returns completed.

Do the same for `_run_tool_loop_interactive_async()`.

- [x] **Step 5: Run focused test**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_stops_when_provider_call_limit_is_reached -q
```

Expected: PASS.

- [x] **Step 6: Run loop tests**

Run:

```bash
pytest tests/test_agent_context_loop.py -q
```

Expected: PASS.

- [x] **Step 7: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 5 of Agent Loop Guardrails. Focus on provider call counting, off-by-one behavior, whether limit responses are persisted once, and whether tool_call/tool_result ordering remains valid when the limit is hit between provider calls.
```

If review passes:

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "feat(agent): enforce provider call limits"
```

---

### Task 6: 单轮总耗时上限

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [x] **Step 1: 写失败测试**

Append:

```python
class FakeClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        if not self.values:
            return 999.0
        return self.values.pop(0)


def test_agent_loop_stops_when_turn_timeout_is_reached(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_turn_timeout", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "one"})],
                finish_reason="tool_calls",
            ),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        limits=AgentLoopLimits(
            max_tool_rounds=None,
            max_provider_calls=None,
            max_turn_seconds=5,
            successful_verification_stop=False,
        ),
        clock=FakeClock([0.0, 0.0, 6.0]),
    ).run_user_turn("调用工具")

    assert response.finish_reason == "turn_timeout"
    assert "本轮任务耗时达到上限" in response.content
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_stops_when_turn_timeout_is_reached -q
```

Expected: FAIL，直到 timeout check 正确接入。

- [x] **Step 3: 确认 timeout check 在每次 provider call 前触发**

Ensure `_complete_once()` and `_stream_once()` call:

```python
        self._check_turn_timeout()
```

before incrementing provider calls and making the provider request.

Ensure `_begin_turn()` is called once per actual user turn before the first provider call.

- [x] **Step 4: Run focused test**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_stops_when_turn_timeout_is_reached -q
```

Expected: PASS.

- [x] **Step 5: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 6 of Agent Loop Guardrails. Focus on deterministic clock injection, timeout check placement, and whether timeout stops before making another provider call.
```

If review passes:

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "feat(agent): enforce turn timeouts"
```

---

### Task 7: max_tool_rounds=None 和工具轮数保险丝

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [x] **Step 1: 写失败测试**

Append:

```python
def test_agent_loop_allows_unlimited_tool_rounds_when_limit_is_none(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_unlimited_tools", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo_1", name="echo", arguments={"text": "one"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo_2", name="echo", arguments={"text": "two"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        limits=AgentLoopLimits(
            max_tool_rounds=None,
            max_provider_calls=10,
            max_turn_seconds=None,
            successful_verification_stop=False,
        ),
    ).run_user_turn("调用两轮工具")

    assert response.content == "done"
    assert response.finish_reason != "tool_round_limit"
```

- [x] **Step 2: 运行测试确认失败或确认当前行为**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_allows_unlimited_tool_rounds_when_limit_is_none -q
```

Expected: If current code compares `tool_rounds >= None`, FAIL. After implementation, PASS.

- [x] **Step 3: 修改工具轮数判断**

In both sync and async loops, replace:

```python
            if tool_rounds >= self.max_tool_rounds:
```

with:

```python
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
```

Replace second check similarly.

- [x] **Step 4: Run focused test and existing limit test**

Run:

```bash
pytest tests/test_agent_context_loop.py::test_agent_loop_allows_unlimited_tool_rounds_when_limit_is_none tests/test_agent_context_loop.py::test_agent_loop_stops_after_max_tool_rounds -q
```

If the existing max tool rounds test has a different name, find it with:

```bash
rg -n "tool_round_limit|max_tool_rounds" tests/test_agent_context_loop.py
```

Expected: PASS.

- [x] **Step 5: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 7 of Agent Loop Guardrails. Focus on max_tool_rounds=None semantics and preserving existing finite max_tool_rounds behavior.
```

If review passes:

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "feat(agent): allow optional tool round limits"
```

---

### Task 8: AgentChatRunner 传递 Loop Limits

**Files:**
- Modify: `firstcoder/app/runtime.py`
- Modify: `firstcoder/app/factory.py`
- Test: `tests/test_app_runtime.py`
- Test: `tests/test_app_factory.py`

- [x] **Step 1: 写失败测试**

Append to `tests/test_app_runtime.py`:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits


def test_chat_runner_passes_loop_limits_to_agent_loop(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_runner_limits", agents_md="")
    state = CurrentSessionState(session)
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    limits = AgentLoopLimits(max_tool_rounds=7, max_provider_calls=8, max_turn_seconds=9)
    runner = AgentChatRunner(current_session=state, provider=provider, limits=limits)

    runner.run_user_turn("hi")

    assert runner.loops[-1].limits == limits
```

- [x] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/test_app_runtime.py::test_chat_runner_passes_loop_limits_to_agent_loop -q
```

Expected: FAIL，因为 `AgentChatRunner` 还没有 `limits` 字段。

- [x] **Step 3: 修改 AgentChatRunner**

In `firstcoder/app/runtime.py`, import:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits
```

Add dataclass field:

```python
    limits: AgentLoopLimits | None = None
```

In every `AgentLoop(...)` construction, pass:

```python
            limits=self.limits,
```

Keep existing:

```python
            max_tool_rounds=self.max_tool_rounds,
```

This preserves old tests and lets explicit `max_tool_rounds` override limits when set.

- [x] **Step 4: 修改 factory 默认值**

In `firstcoder/app/factory.py`, import:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits
```

When constructing `AgentChatRunner`, pass:

```python
        limits=AgentLoopLimits.default(),
```

- [x] **Step 5: Add app factory assertion**

Append to `tests/test_app_factory.py`:

```python
def test_app_factory_configures_default_loop_limits(tmp_path) -> None:
    app = create_firstcoder_app(project_root=tmp_path, provider=FakeProvider([]))

    assert app.chat_runner.limits == AgentLoopLimits.default()
```

If `AgentLoopLimits` / `FakeProvider` imports are missing, add them at the top of `tests/test_app_factory.py`:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits
```

- [x] **Step 6: Run app tests**

Run:

```bash
pytest tests/test_app_runtime.py tests/test_app_factory.py -q
```

Expected: PASS.

- [x] **Step 7: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 8 of Agent Loop Guardrails. Focus on whether AgentChatRunner forwards limits in sync, async, and resume paths, and whether factory defaults match the goal plan.
```

If review passes:

```bash
git add firstcoder/app/runtime.py firstcoder/app/factory.py tests/test_app_runtime.py tests/test_app_factory.py
git commit -m "feat(app): configure agent loop limits"
```

---

### Task 9: 更新 SWE Lite 计划中的预算默认值

**Files:**
- Modify: `docs/SWE_LITE_IMPLEMENTATION_PLAN.md`

- [x] **Step 1: 修改 SWE Lite 计划**

Find the `FirstCoderCodingAgentAdapter` implementation section in `docs/SWE_LITE_IMPLEMENTATION_PLAN.md`.

Update adapter imports in the plan's code block to include:

```python
from firstcoder.agent.loop_limits import AgentLoopLimits
```

Update `AgentLoop(...)` construction in the plan's code block to:

```python
        return AgentLoop(
            session=session,
            provider=create_provider(self.provider_name),
            tools=list(registry),
            limits=AgentLoopLimits.swe_lite(),
        )
```

Add a short note under implementation notes:

```markdown
- SWE-bench Lite runs should use `AgentLoopLimits.swe_lite()` so the agent has enough budget for real debugging while still stopping on provider-call and wall-clock limits.
```

- [x] **Step 2: Verify diff**

Run:

```bash
git diff -- docs/SWE_LITE_IMPLEMENTATION_PLAN.md
```

Expected: diff only updates loop budget guidance.

- [x] **Step 3: 子代理审阅并提交**

Dispatch review subagent:

```text
Review Task 9 of Agent Loop Guardrails. Focus on whether the SWE Lite plan now uses the new loop limits without adding benchmark-specific logic to AgentLoop.
```

If review passes:

```bash
git add docs/SWE_LITE_IMPLEMENTATION_PLAN.md
git commit -m "docs(eval): align swe lite plan with loop guardrails"
```

---

### Task 10: Final Verification

**Files:**
- No new files unless tests reveal a required fix.

- [x] **Step 1: Run focused test set**

Run:

```bash
pytest tests/test_agent_verification.py tests/test_agent_loop_limits.py tests/test_agent_context_loop.py tests/test_app_runtime.py tests/test_app_factory.py -q
```

Expected: PASS.

- [x] **Step 2: Run full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 3: Run local smoke with real provider if API config is available**

Use the Python 3.12 runtime if system `python3` is 3.9:

```bash
/Users/x/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.grants import PermissionGrantStore, PermissionGrant
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import PermissionAction, PermissionMode, PermissionScopeType
from firstcoder.providers.factory import create_provider
from firstcoder.tools.builtin import create_builtin_registry

PYTHON = "/Users/x/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

def run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)

with TemporaryDirectory(prefix="firstcoder-loop-guardrails-smoke-") as tmp:
    root = Path(tmp) / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    run(["git", "init"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    run(["git", "config", "user.name", "Test User"], root)
    run(["git", "add", "calc.py", "test_calc.py"], root)
    run(["git", "commit", "-m", "init"], root)

    grants = PermissionGrantStore([
        PermissionGrant(
            id="allow-write-root",
            effect="allow",
            action=PermissionAction.WRITE_PATH,
            scope_type=PermissionScopeType.PATH_TREE,
            scope_value=str(root),
            created_at="2026-06-21T00:00:00+00:00",
            reason="smoke write",
        ),
        PermissionGrant(
            id="allow-read-root",
            effect="allow",
            action=PermissionAction.READ_PATH,
            scope_type=PermissionScopeType.PATH_TREE,
            scope_value=str(root),
            created_at="2026-06-21T00:00:00+00:00",
            reason="smoke read",
        ),
        PermissionGrant(
            id="allow-pytest",
            effect="allow",
            action=PermissionAction.EXECUTE_SHELL,
            scope_type=PermissionScopeType.COMMAND_PREFIX,
            scope_value=f"{PYTHON} -m pytest",
            created_at="2026-06-21T00:00:00+00:00",
            reason="smoke tests",
        ),
    ])
    registry = create_builtin_registry(root, include_mutation_tools=True, include_execution_tools=True)
    store = JsonlSessionStore(Path(tmp) / "sessions")
    session = AgentSession.from_project(
        store=store,
        session_id="loop-guardrails-smoke",
        project_root=root,
        tools=registry.tools(),
        permission_manager=PermissionManager(
            policy=DefaultPermissionPolicy(root),
            grants=grants,
            mode=PermissionMode.AGGRESSIVE,
        ),
    )
    response = AgentLoop(
        session=session,
        provider=create_provider(),
        tools=registry.tools(),
        limits=AgentLoopLimits(max_tool_rounds=60, max_provider_calls=30, max_turn_seconds=600),
    ).run_user_turn(
        f"Fix the project. Do not edit tests. Run `{PYTHON} -m pytest -q` and stop when tests pass."
    )
    after = run([PYTHON, "-m", "pytest", "-q"], root, check=False)
    print("finish=", response.finish_reason)
    print("response=", response.content.strip()[:500])
    print("pytest_exit=", after.returncode)
    print("pytest_output=", (after.stdout + after.stderr).strip())
PY
```

Expected:

```text
pytest_exit= 0
finish= stop
```

The final response should not be `tool_round_limit`, `provider_call_limit`, or `turn_timeout`.

- [x] **Step 4: Final review and commit if any verification fixes were needed**

If final verification required code changes:

```bash
git add <changed-files>
git commit -m "fix(agent): stabilize loop guardrail verification"
```

---

## Self-Review Checklist

Before executing this plan, verify:

- Every goal in `docs/AGENT_LOOP_GUARDRAILS_GOAL.md` maps to a task above.
- No benchmark-specific condition is hard-coded into `AgentLoop`.
- Successful verification only triggers for successful verification commands.
- Failed tests continue the normal loop.
- Provider-call and timeout failures produce clear `finish_reason` values.
- Existing permission confirmation and skipped tool result behavior are preserved.
- The SWE Lite plan uses `AgentLoopLimits.swe_lite()`.
