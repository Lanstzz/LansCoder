# 子 agent 停止面板 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让前台/后台子 agent 真正可被取消，并给 TUI 底部子 agent 面板加上"选中高亮 + `x` 停止 + Esc 返回"的交互（对齐 Claude Code 的 `/tasks`）。

**Architecture:** 两段式。Part A：子 `AgentLoop` 通过 `current_cancellation_token()` 接上父 token（前台）或 job token（后台），取消信号真正到达子 agent，`AgentCancelledError` 直接上抛而非包装成失败。Part B：把面板选择状态机抽成纯模块 `subagent_panel_state.py`（可按稳定 id 移动/进入/停止），Textual 层保持薄，`tui.py` 只做状态、渲染、按键与鼠标接线。

**Tech Stack:** Python 3、pytest、Textual（TUI）、asyncio/anyio、线程（BackgroundJobManager 的 ThreadPoolExecutor）。

**Spec:** [docs/superpowers/specs/2026-08-20-subagent-stop-panel-design.md](../specs/2026-08-20-subagent-stop-panel-design.md)

## Global Constraints

- 开发 venv 为 `.venv`，测试用 `.venv/bin/python -m pytest`；改完文件跑 `ruff check` + `ruff format`。
- 子 agent 取消必须是"中断"语义（`AgentCancelledError`），不得包装成 `Subagent failed`。
- 停止前台子 agent 的效果 = 中断当前 turn（架构固有限制，spec 已确认接受）。
- 面板选择按稳定 id（前台 `"fg"`、后台 `job.id`）而非下标。
- 停止与"中断 turn"语义分离：`x` 停止选中项；Esc 退出选择模式；未选中时 Esc 双击仍中断 turn。
- 每任务完成后 commit（用户已授权子 agent 开发期 commit）；最终合入需用户审核。
- 遵循 spec 第 5 节验收标准：全量 `pytest -q` 全绿（既有 `test_mcp_integration.py` 环境性失败除外）+ `ruff check` + `ruff format --check`。

---

### Task 1: `_run_inline` 取消链路（前台 + 后台 inline 子 agent）

**Files:**
- Modify: `lanscoder/agent/subagent.py`（顶部 import + `_run_inline`）
- Test: `tests/test_delegate_tool.py`

**Interfaces:**
- Consumes: `AgentCancelledError`, `current_cancellation_token`（`lanscoder.runtime.cancellation`）；子 `AgentLoop` 的 `cancellation_token` 参数（loop.py:119）；`response.finish_reason == "interrupted"`（`_interrupted_response`，loop.py:1308）。
- Produces: `SubagentRunner._run_inline` 在子 agent 被取消时抛 `AgentCancelledError`（不再返回 `ok=False, error="Agent turn was interrupted."`）。

- [ ] **Step 1: 写前台取消回归测试**（追加到 `tests/test_delegate_tool.py`，放在 `test_foreground_delegate_survives_background_delegate_finish` 之后）

```python
def test_foreground_delegate_cancel_aborts_child(tmp_path) -> None:
    """Foreground subagent must abort with AgentCancelledError when the parent turn
    is cancelled — it must not run to completion and be mislabelled as failed."""

    from lanscoder.runtime.cancellation import (
        AgentCancelledError,
        CancellationToken,
        cancellation_context,
    )

    store = JsonlSessionStore(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if (
                request.tools == []
                and request.tool_choice == "none"
                and request.max_tokens == 512
            ):
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="child done")

    runner = SubagentRunner(
        store=store, provider=BlockingProvider([]), tools=[_tool("view")]
    )
    token = CancellationToken()
    outcomes: list[object] = []

    def fg_func() -> None:
        with cancellation_context(token):
            try:
                runner.run(
                    SubagentRequest(
                        role="researcher",
                        task="foreground task",
                        parent_session_id="p_fg",
                    )
                )
            except AgentCancelledError as exc:
                outcomes.append(exc)

    fg_thread = threading.Thread(target=fg_func)
    fg_thread.start()
    assert started.wait(5), "foreground child should start and block on its provider"
    token.cancel()  # user interrupts the turn mid-subagent
    release.set()
    fg_thread.join(10)
    assert not fg_thread.is_alive(), "foreground delegate should abort after cancel"
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], AgentCancelledError)
```

- [ ] **Step 2: 写后台 inline 取消回归测试**（同一个测试文件，紧接上一个函数）

```python
def test_background_delegate_cancel_aborts_child(tmp_path) -> None:
    """A cancelled background job must abort its inline subagent with
    AgentCancelledError and finish cancelled, not run to completion."""

    from lanscoder.runtime.cancellation import AgentCancelledError

    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if (
                request.tools == []
                and request.tool_choice == "none"
                and request.max_tokens == 512
            ):
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="child done")

    runner = SubagentRunner(
        store=store,
        provider=BlockingProvider([]),
        tools=[_tool("view")],
        background_manager=manager,
    )
    outcomes: list[object] = []

    def job_func() -> ToolResult:
        try:
            runner.run(
                SubagentRequest(
                    role="researcher",
                    task="background task",
                    parent_session_id="p_bg",
                )
            )
        except AgentCancelledError as exc:
            outcomes.append(exc)
        return make_text_result("delegate", "done")

    try:
        job = manager.start(job_func, tool_name="delegate")
        assert started.wait(5), "background child should start and block on its provider"
        manager.cancel(job.id)
        release.set()
        assert manager.wait(timeout=5) is True
    finally:
        manager.shutdown()

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], AgentCancelledError)
    assert job.status == "cancelled"
```

- [ ] **Step 3: 运行两个新测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_delegate_tool.py::test_foreground_delegate_cancel_aborts_child tests/test_delegate_tool.py::test_background_delegate_cancel_aborts_child -q`
Expected: 两个都 FAIL。当前代码子 agent 跑完返回 `ok=True`，`outcomes` 为空。

- [ ] **Step 4: 实现取消链路**（`lanscoder/agent/subagent.py`）

顶部 import 块加一行：

```python
from lanscoder.runtime.cancellation import AgentCancelledError, current_cancellation_token
```

`_run_inline` 里构造子 `AgentLoop` 时加 `cancellation_token`：

```python
            loop = AgentLoop(
                session=child_session,
                provider=self.provider,
                tools=self.tools_for_role(request.role),
                limits=self.limits,
                request_options=self.request_options,
                background_manager=None,
                enable_delegate_tool=False,
                progress_callback=self._make_progress_callback(progress_tracker),
                cancellation_token=current_cancellation_token(),
            )
```

`_run_inline` 的子 loop try/except 改为（在宽 except 前加 `AgentCancelledError` 分支，并把 interrupted 响应转成异常）：

```python
            try:
                result = asyncio.run(loop.run_user_turn(prompt))
                response = result.response
                if response is None:
                    raise RuntimeError("subagent paused for user input")
                if response.finish_reason == "interrupted":
                    raise AgentCancelledError()
            except AgentCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - delegate must return a tool result, not break parent loop
```

- [ ] **Step 5: 运行测试确认通过**

Run: `tests/test_delegate_tool.py::test_foreground_delegate_cancel_aborts_child tests/test_delegate_tool.py::test_background_delegate_cancel_aborts_child -q`
Expected: 2 passed。

- [ ] **Step 6: 跑既有 delegate 用例 + ruff**

Run: `.venv/bin/python -m pytest tests/test_delegate_tool.py -q && .venv/bin/ruff check lanscoder/agent/subagent.py tests/test_delegate_tool.py && .venv/bin/ruff format lanscoder/agent/subagent.py tests/test_delegate_tool.py`
Expected: 全部 passed，ruff 干净。

- [ ] **Step 7: Commit**

```bash
git add lanscoder/agent/subagent.py tests/test_delegate_tool.py
git commit -m "feat: make inline subagent loops cancellable via parent or job token"
```

---

### Task 2: `_run_isolated` 取消链路（后台 coder / worktree 隔离）

**Files:**
- Modify: `lanscoder/agent/subagent.py`（`_run_isolated`）
- Test: `tests/test_delegate_tool.py`

**Interfaces:**
- Consumes: Task 1 的 import 与 `AgentCancelledError` 语义；`_init_git_repo`（test 文件已有的模块级 helper）；`SubagentRequest(isolate_worktree=True)`。
- Produces: `SubagentRunner._run_isolated` 在子 agent 被取消时抛 `AgentCancelledError`（不包装成"隔离 coder 执行失败"或"隔离执行初始化失败"）。

- [ ] **Step 1: 写后台 coder 取消回归测试**（追加到 `tests/test_delegate_tool.py`，放在 `test_background_coder_uses_worktree_and_leaves_parent_untouched` 之后）

```python
def test_isolated_coder_cancel_aborts_child(tmp_path) -> None:
    """A cancelled background coder must abort its worktree-isolated subagent with
    AgentCancelledError and finish cancelled, not run to completion."""

    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode
    from lanscoder.runtime.cancellation import AgentCancelledError

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if (
                request.tools == []
                and request.tool_choice == "none"
                and request.max_tokens == 512
            ):
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="coder done")

    store = JsonlSessionStore(repo / ".fc_sessions")
    runner = SubagentRunner(
        store=store,
        provider=BlockingProvider([]),
        tools=[],
        project_root=repo,
        permission_manager=PermissionManager(
            policy=DefaultPermissionPolicy(repo), mode=PermissionMode.STANDARD
        ),
        background_manager=manager,
    )
    outcomes: list[object] = []

    def job_func() -> ToolResult:
        try:
            runner.run(
                SubagentRequest(
                    role="coder",
                    task="edit in isolation",
                    parent_session_id="p_coder",
                    isolate_worktree=True,
                )
            )
        except AgentCancelledError as exc:
            outcomes.append(exc)
        return make_text_result("delegate", "done")

    try:
        job = manager.start(job_func, tool_name="delegate")
        assert started.wait(5), "isolated child should start and block on its provider"
        manager.cancel(job.id)
        release.set()
        assert manager.wait(timeout=5) is True
    finally:
        manager.shutdown()

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], AgentCancelledError)
    assert job.status == "cancelled"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_delegate_tool.py::test_isolated_coder_cancel_aborts_child -q`
Expected: FAIL（子 agent 跑完返回 `ok=True`，`outcomes` 为空）。

- [ ] **Step 3: 实现 `_run_isolated` 取消链路**

在 `_run_isolated` 构造子 `AgentLoop` 时加 `cancellation_token`：

```python
                loop = AgentLoop(
                    session=child_session,
                    provider=self.provider,
                    tools=self._worktree_child_tools(
                        worktree.path, profile=profile, access=child_session.sandbox_access
                    ),
                    limits=self.limits,
                    request_options=self.request_options,
                    background_manager=None,
                    enable_delegate_tool=False,
                    progress_callback=self._make_progress_callback(progress_tracker),
                    cancellation_token=current_cancellation_token(),
                )
```

子 loop 的内层 try/except（`asyncio.run(loop.run_user_turn(prompt))` 那段）改为：

```python
                try:
                    result = asyncio.run(loop.run_user_turn(prompt))
                    response = result.response
                    if response is None:
                        diff = manager.diff(worktree)
                        usage = loop.usage_summary()
                        return SubagentResult(
                            ok=False,
                            role=request.role,
                            child_session_id=session_id,
                            summary="隔离 coder 等待用户输入，无法在后台继续。",
                            error="waiting_for_user_input",
                            files_changed=diff.files_changed,
                            worktree_path=str(worktree.path),
                            worktree_branch=worktree.branch,
                            diff_summary=diff.render(),
                            total_tokens=usage["total_tokens"],
                            provider_calls=usage["provider_calls"],
                            elapsed_seconds=time.monotonic() - started,
                        )
                    if response.finish_reason == "interrupted":
                        raise AgentCancelledError()
                except AgentCancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never break the parent loop
```

外层防御性 `except Exception`（"隔离执行初始化失败"）前加 `AgentCancelledError` 分支：

```python
        except AgentCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - defensive: setup failures must not break parent loop
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_delegate_tool.py::test_isolated_coder_cancel_aborts_child -q`
Expected: PASS。

- [ ] **Step 5: 跑既有隔离用例 + ruff**

Run: `.venv/bin/python -m pytest tests/test_delegate_tool.py -q && .venv/bin/ruff check lanscoder/agent/subagent.py tests/test_delegate_tool.py && .venv/bin/ruff format lanscoder/agent/subagent.py tests/test_delegate_tool.py`
Expected: 全部 passed，ruff 干净。

- [ ] **Step 6: Commit**

```bash
git add lanscoder/agent/subagent.py tests/test_delegate_tool.py
git commit -m "feat: make worktree-isolated subagent loops cancellable via job token"
```

---

### Task 3: 纯状态机模块 `subagent_panel_state`

**Files:**
- Create: `lanscoder/app/subagent_panel_state.py`
- Create: `tests/test_subagent_panel_state.py`

**Interfaces:**
- Produces（Task 4/5 消费）：
  - `FG_ID = "fg"`
  - `@dataclass(frozen=True) class SubagentRow: id: str; label: str; status: str; cancellable: bool; cancel_requested: bool`
  - `build_rows(foreground: dict[str, Any] | None, jobs: list[Any]) -> list[SubagentRow]`
  - `move_selection(rows, selected: str | None, direction: str) -> str | None`
  - `can_enter_selection(rows, down_recall: str | None) -> bool`
  - `stop_target(rows, selected: str | None) -> str | None`
  - `has_running(rows) -> bool`

- [ ] **Step 1: 写模块测试**（`tests/test_subagent_panel_state.py`）

```python
from __future__ import annotations

from lanscoder.app.subagent_panel_state import (
    FG_ID,
    SubagentRow,
    build_rows,
    can_enter_selection,
    has_running,
    move_selection,
    stop_target,
)


def _row(
    row_id: str,
    *,
    status: str = "running",
    cancellable: bool = True,
    cancel_requested: bool = False,
) -> SubagentRow:
    return SubagentRow(
        id=row_id,
        label=row_id,
        status=status,
        cancellable=cancellable,
        cancel_requested=cancel_requested,
    )


class _Job:
    def __init__(
        self,
        job_id: str,
        label: str | None,
        *,
        status: str = "running",
        cancel_requested: bool = False,
    ) -> None:
        self.id = job_id
        self.label = label
        self.tool_name = "delegate"
        self.status = status
        self.cancel_requested = cancel_requested


def test_build_rows_puts_foreground_first() -> None:
    fg = {"label": "researcher", "started_at": 0.0, "provider_calls": 0, "total_tokens": 0}
    rows = build_rows(fg, [_Job("bg_0001", "reviewer")])
    assert [row.id for row in rows] == ["fg", "bg_0001"]
    assert rows[0].label == "researcher"
    assert rows[1].id == "bg_0001"
    assert rows[1].cancellable is True


def test_build_rows_empty_when_nothing_running() -> None:
    assert build_rows(None, []) == []


def test_build_rows_labels_cancelling_jobs() -> None:
    rows = build_rows(None, [_Job("bg_0001", "reviewer", cancel_requested=True)])
    assert rows[0].status == "cancelling"
    assert rows[0].cancel_requested is True


def test_move_selection_clamps_at_edges() -> None:
    rows = [_row(FG_ID), _row("bg_0001")]
    assert move_selection(rows, None, "down") == FG_ID
    assert move_selection(rows, FG_ID, "down") == "bg_0001"
    assert move_selection(rows, "bg_0001", "down") == "bg_0001"
    assert move_selection(rows, FG_ID, "up") == FG_ID
    assert move_selection(rows, None, "up") == "bg_0001"


def test_move_selection_relocates_when_selected_dropped() -> None:
    rows = [_row(FG_ID), _row("bg_0001")]
    assert move_selection(rows, "gone", "down") == FG_ID


def test_can_enter_selection_requires_running_and_nothing_to_recall() -> None:
    assert can_enter_selection([_row(FG_ID)], None) is True
    assert can_enter_selection([_row(FG_ID)], "") is True
    assert can_enter_selection([_row(FG_ID)], "older text") is False
    assert can_enter_selection([], None) is False
    assert can_enter_selection([_row(FG_ID, cancellable=False)], None) is False


def test_has_running() -> None:
    assert has_running([_row(FG_ID)]) is True
    assert has_running([_row(FG_ID, cancellable=False)]) is False
    assert has_running([]) is False


def test_stop_target_returns_selected_cancellable_row() -> None:
    rows = [_row(FG_ID), _row("bg_0001", cancellable=False)]
    assert stop_target(rows, FG_ID) == FG_ID
    assert stop_target(rows, "bg_0001") is None
    assert stop_target(rows, None) is None
    assert stop_target(rows, "missing") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_subagent_panel_state.py -q`
Expected: 全部 FAIL（ModuleNotFoundError: lanscoder.app.subagent_panel_state）。

- [ ] **Step 3: 实现模块**（`lanscoder/app/subagent_panel_state.py`）

```python
"""Subagent panel selection state machine (pure, TUI-agnostic).

Selection logic lives here so it is unit-testable without a running Textual
app.  Stable ids: the foreground subagent is ``FG_ID``; each background job
uses its own job id.  Stopping a foreground subagent ends the parent turn
(the two share a thread and cancellation token) — that is an architectural
constraint, not a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FG_ID = "fg"


@dataclass(frozen=True)
class SubagentRow:
    id: str
    label: str
    status: str
    cancellable: bool
    cancel_requested: bool


def build_rows(
    foreground: dict[str, Any] | None, jobs: list[Any]
) -> list[SubagentRow]:
    rows: list[SubagentRow] = []
    if foreground is not None:
        rows.append(
            SubagentRow(
                id=FG_ID,
                label=foreground.get("label") or "delegate",
                status="running",
                cancellable=True,
                cancel_requested=False,
            )
        )
    for job in jobs:
        rows.append(
            SubagentRow(
                id=job.id,
                label=job.label or job.tool_name,
                status="cancelling" if job.cancel_requested else job.status,
                cancellable=job.status == "running",
                cancel_requested=job.cancel_requested,
            )
        )
    return rows


def has_running(rows: list[SubagentRow]) -> bool:
    return any(row.cancellable for row in rows)


def move_selection(
    rows: list[SubagentRow], selected: str | None, direction: str
) -> str | None:
    if not rows:
        return None
    if selected is None:
        return rows[0].id if direction == "down" else rows[-1].id
    index_by_id = {row.id: i for i, row in enumerate(rows)}
    index = index_by_id.get(selected)
    if index is None:
        return rows[0].id
    if direction == "down":
        index = min(len(rows) - 1, index + 1)
    elif direction == "up":
        index = max(0, index - 1)
    return rows[index].id


def can_enter_selection(
    rows: list[SubagentRow], down_recall: str | None
) -> bool:
    """Down arrow may enter selection mode when the input history has nothing
    newer to recall (``down_recall`` is None or empty) and a subagent is running."""
    return not down_recall and has_running(rows)


def stop_target(rows: list[SubagentRow], selected: str | None) -> str | None:
    for row in rows:
        if row.id == selected and row.cancellable:
            return row.id
    return None
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_subagent_panel_state.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: ruff**

Run: `.venv/bin/ruff check lanscoder/app/subagent_panel_state.py tests/test_subagent_panel_state.py && .venv/bin/ruff format lanscoder/app/subagent_panel_state.py tests/test_subagent_panel_state.py`
Expected: 干净。

- [ ] **Step 6: Commit**

```bash
git add lanscoder/app/subagent_panel_state.py tests/test_subagent_panel_state.py
git commit -m "feat: add pure subagent panel selection state machine"
```

---

### Task 4: TUI 面板渲染选中态 + 提示 + cancelling + CSS

**Files:**
- Modify: `lanscoder/app/tui.py`（`__init__` 状态、`_refresh_subagent_progress`、新增 `_subagent_rows`、`_sync_subagent_selection`）
- Modify: `lanscoder/app/tui.tcss`
- Test: `tests/test_app_tui.py`

**Interfaces:**
- Consumes: Task 3 的 `FG_ID`、`build_rows`、`SubagentRow`；既有 `_format_subagent_line` / `_progress_indicator` / `_MAX_VISIBLE_SUBAGENT_LINES`（tui.py）。
- Produces: 面板渲染时给选中行加 `.selected` 类、底部提示行（`#subagent-hint`）、`cancelling` 状态显示；App 状态 `_subagent_selected: str | None`、`_subagent_select_mode: bool`。

- [ ] **Step 1: 写面板渲染测试**（追加到 `tests/test_app_tui.py`，放在 `test_lanscoder_app_can_be_created_with_command_handler` 之后）

```python
class _FakeForegroundChatRunner:
    def __init__(self) -> None:
        self.background_manager = None
        self.interrupted_turns = 0
        self._foreground = {
            "label": "researcher",
            "started_at": 0.0,
            "provider_calls": 0,
            "total_tokens": 0,
        }

    def foreground_subagent(self) -> dict | None:
        return self._foreground

    def cancel_current_turn(self) -> None:
        self.interrupted_turns += 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_highlights_selected_row_and_shows_hint() -> None:
    app = LansCoderApp(chat_runner=_FakeForegroundChatRunner())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._subagent_select_mode = True
        app._subagent_selected = "fg"
        app._refresh_subagent_progress()
        panel = app.query_one("#subagent-panel")
        selected = [s for s in panel.query("Static") if s.has_class("selected")]
        assert [s.id for s in selected] == ["subagent-row-fg"]
        hint = app.query_one("#subagent-hint")
        assert "x 停止" in str(hint.renderable)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py::test_subagent_panel_highlights_selected_row_and_shows_hint -q`
Expected: FAIL（当前渲染无 `.selected` 类、无 `#subagent-hint`）。

- [ ] **Step 3: 实现渲染与状态**

`tui.py` 顶部 import 加：

```python
from lanscoder.app.subagent_panel_state import (
    FG_ID,
    SubagentRow,
    build_rows,
)
```

`__init__`（在 `self._activity_frame = 0` 附近）加状态：

```python
        self._subagent_selected: str | None = None
        self._subagent_select_mode = False
```

把 `_refresh_subagent_progress` 整体替换为：

```python
    def _refresh_subagent_progress(self) -> None:
        """Periodic timer: render running sub-agents (foreground first, then background)."""
        manager = None
        foreground = None
        if self.chat_runner is not None:
            manager = getattr(self.chat_runner, "background_manager", None)
            foreground = getattr(self.chat_runner, "foreground_subagent", lambda: None)()
        try:
            panel = self.query_one("#subagent-panel")
        except Exception:
            return
        jobs = manager.active_jobs() if manager is not None else []
        rows = build_rows(foreground, jobs)
        self._sync_subagent_selection(rows)
        panel.remove_children()
        if not rows:
            panel.add_class("hidden")
            return
        panel.remove_class("hidden")
        self._activity_frame += 1
        indicator = _progress_indicator(self._activity_frame)
        now = time.monotonic()
        lines_by_id: dict[str, str] = {}
        if foreground is not None:
            lines_by_id[FG_ID] = _format_subagent_line(
                label=foreground.get("label") or "delegate",
                elapsed=now - foreground["started_at"],
                calls=foreground.get("provider_calls", 0),
                tokens=foreground.get("total_tokens", 0),
                indicator=indicator,
            )
        for job in jobs:
            progress = job.progress or {}
            line = _format_subagent_line(
                label=job.label or job.tool_name,
                elapsed=now - job.created_at,
                calls=progress.get("provider_calls", 0),
                tokens=progress.get("total_tokens", 0),
                indicator=indicator,
            )
            if job.cancel_requested:
                line = f"{line} · cancelling"
            lines_by_id[job.id] = line
        for row in rows[:_MAX_VISIBLE_SUBAGENT_LINES]:
            static = Static(lines_by_id[row.id], id=f"subagent-row-{row.id}")
            if row.id == self._subagent_selected:
                static.add_class("selected")
            panel.mount(static)
        hidden = len(rows) - _MAX_VISIBLE_SUBAGENT_LINES
        if hidden > 0:
            panel.mount(Static(f"…还有 {hidden} 个子agent在跑"))
        panel.mount(
            Static(
                "↑/↓ 选择 · x 停止 · Esc 返回"
                if self._subagent_select_mode
                else "↓ 进入选择 · 点击选择子agent",
                id="subagent-hint",
                classes="subagent-hint",
            )
        )

    def _subagent_rows(self) -> list[SubagentRow]:
        manager = None
        foreground = None
        if self.chat_runner is not None:
            manager = getattr(self.chat_runner, "background_manager", None)
            foreground = getattr(self.chat_runner, "foreground_subagent", lambda: None)()
        jobs = manager.active_jobs() if manager is not None else []
        return build_rows(foreground, jobs)

    def _sync_subagent_selection(self, rows: list[SubagentRow]) -> None:
        if self._subagent_selected is not None and not any(
            row.id == self._subagent_selected for row in rows
        ):
            self._subagent_selected = None
            self._subagent_select_mode = False
```

`tui.tcss` 的 `#subagent-panel Static` 块后追加：

```css
#subagent-panel Static.selected {
    background: #2b5fd9;
    color: #ffffff;
    text-style: bold;
}
#subagent-panel .subagent-hint {
    color: #5a5c64;
    text-style: dim;
}
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py::test_subagent_panel_highlights_selected_row_and_shows_hint -q`
Expected: PASS。

- [ ] **Step 5: 跑既有 TUI 用例 + ruff**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py -q && .venv/bin/ruff check lanscoder/app/tui.py lanscoder/app/tui.tcss tests/test_app_tui.py && .venv/bin/ruff format lanscoder/app/tui.py tests/test_app_tui.py`
Expected: 全部 passed，ruff 干净。

- [ ] **Step 6: Commit**

```bash
git add lanscoder/app/tui.py lanscoder/app/tui.tcss tests/test_app_tui.py
git commit -m "feat: render selected subagent rows with highlight and binding hint"
```

---

### Task 5: TUI 按键 + 停止动作 + 鼠标点击

**Files:**
- Modify: `lanscoder/app/tui.py`（`on_key`、新增 `_handle_subagent_select_key`、`_enter_subagent_selection`、`_stop_selected_subagent`、`on_click`）
- Test: `tests/test_app_tui.py`

**Interfaces:**
- Consumes: Task 3 的 `move_selection`、`can_enter_selection`、`stop_target`；Task 4 的 `_subagent_rows`、`_subagent_select_mode`、`_subagent_selected`、`FG_ID`；`chat_runner.cancel_current_turn()` 与 `chat_runner.background_manager.cancel(job_id)`。
- Produces: 选择模式下 ↑/↓ 移动、`x` 停止选中项、Esc 退出选择；普通模式下 down 进入选择；鼠标点击面板行选中并进入选择。

- [ ] **Step 1: 写交互测试**（追加到 `tests/test_app_tui.py`，放在 `test_subagent_panel_highlights_selected_row_and_shows_hint` 之后；`make_text_result` 从 `lanscoder.tools.types` 导入）

```python
from lanscoder.tools.types import make_text_result  # 追加到顶部 import 块


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_x_stops_foreground_turn() -> None:
    fake = _FakeForegroundChatRunner()
    app = LansCoderApp(chat_runner=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_subagent_progress()
        await pilot.press("down")
        assert app._subagent_select_mode is True
        assert app._subagent_selected == "fg"
        await pilot.press("x")
        assert fake.interrupted_turns == 1
        await pilot.press("escape")
        assert app._subagent_select_mode is False


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_x_stops_background_job() -> None:
    gate = threading.Event()

    class _FakeManagerChatRunner:
        def __init__(self, manager) -> None:
            self.background_manager = manager

        def foreground_subagent(self) -> dict | None:
            return None

        def cancel_current_turn(self) -> None:
            pass

    manager = BackgroundJobManager()
    fake = _FakeManagerChatRunner(manager)
    app = LansCoderApp(chat_runner=fake)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            job = manager.start(
                lambda: gate.wait(5) or make_text_result("delegate", "done"),
                tool_name="delegate",
            )
            app._refresh_subagent_progress()
            await pilot.press("down")
            assert app._subagent_selected == job.id
            await pilot.press("x")
            assert job.cancel_requested is True
            gate.set()
    finally:
        gate.set()
        manager.wait(timeout=5)
        manager.shutdown()
```

- [ ] **Step 2: 运行确认失败**

Run: `tests/test_app_tui.py::test_subagent_panel_x_stops_foreground_turn tests/test_app_tui.py::test_subagent_panel_x_stops_background_job -q`
Expected: 两个都 FAIL（当前 down 不进入选择模式，`_subagent_select_mode` 不存在/False）。

- [ ] **Step 3: 实现按键与动作**

`tui.py` 顶部 import 追加 `move_selection`、`can_enter_selection`、`stop_target`：

```python
from lanscoder.app.subagent_panel_state import (
    FG_ID,
    SubagentRow,
    build_rows,
    can_enter_selection,
    move_selection,
    stop_target,
)
```

`on_key` 改为（在 picker 分支后插入选择模式处理，并给 down 增加进入逻辑）：

```python
    def on_key(self, event: Key) -> None:
        if self._picker is not None and self._handle_picker_key(event):
            event.stop()
            event.prevent_default()
            return
        if self._subagent_select_mode:
            if self._handle_subagent_select_key(event):
                event.stop()
                event.prevent_default()
            return
        if event.key == "escape":
            if self._handle_escape_interrupt():
                event.stop()
                event.prevent_default()
            return
        if event.key not in {"up", "down"}:
            return
        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return
        input_widget = self.query_one("#input", TextArea)
        recalled = self._recall_input_history(event.key)
        if event.key == "down" and can_enter_selection(
            self._subagent_rows(), recalled
        ):
            self._enter_subagent_selection()
            event.stop()
            event.prevent_default()
            return
        if recalled is None:
            return
        event.stop()
        event.prevent_default()
        input_widget.load_text(recalled)
        input_widget.cursor_location = input_widget.document.end
```

新增三个方法（放在 `_recall_input_history` 之前）：

```python
    def _handle_subagent_select_key(self, event: Key) -> bool:
        """选择模式下的按键：↑/↓ 移动、x 停止、Esc 返回；其它键退出选择并交由原逻辑。"""
        if event.key == "escape":
            self._subagent_select_mode = False
            return True
        if event.key in {"up", "down"}:
            self._subagent_selected = move_selection(
                self._subagent_rows(), self._subagent_selected, event.key
            )
            self._refresh_subagent_progress()
            return True
        if event.key == "x":
            self._stop_selected_subagent()
            return True
        self._subagent_select_mode = False
        return False

    def _enter_subagent_selection(self) -> None:
        self._subagent_select_mode = True
        self._subagent_selected = move_selection(
            self._subagent_rows(), None, "down"
        )
        self._refresh_subagent_progress()

    def _stop_selected_subagent(self) -> None:
        rows = self._subagent_rows()
        target = stop_target(rows, self._subagent_selected)
        if target is None:
            return
        if target == FG_ID:
            cancel = getattr(self.chat_runner, "cancel_current_turn", None)
            if cancel is not None:
                cancel()
            return
        manager = (
            getattr(self.chat_runner, "background_manager", None)
            if self.chat_runner is not None
            else None
        )
        if manager is not None:
            manager.cancel(target)
```

新增 `on_click`（放在 `on_paste` 之前）：

```python
    def on_click(self, event: events.Click) -> None:
        widget_id = getattr(event.widget, "id", None)
        if isinstance(widget_id, str) and widget_id.startswith("subagent-row-"):
            self._subagent_selected = widget_id[len("subagent-row-") :]
            self._subagent_select_mode = True
            self._refresh_subagent_progress()
            event.stop()
```

（`from textual import events` 已在 `tests/test_app_tui.py` 顶部，但 `tui.py` 需要检查是否已导入 `events`；若无则加 `from textual import events`。）

- [ ] **Step 4: 运行确认通过**

Run: `tests/test_app_tui.py::test_subagent_panel_x_stops_foreground_turn tests/test_app_tui.py::test_subagent_panel_x_stops_background_job -q`
Expected: 两个都 PASS。

- [ ] **Step 5: 全量验证**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check lanscoder/app/tui.py lanscoder/app/tui.tcss lanscoder/app/subagent_panel_state.py tests/test_app_tui.py tests/test_subagent_panel_state.py && .venv/bin/ruff format lanscoder/app/tui.py lanscoder/app/subagent_panel_state.py tests/test_app_tui.py tests/test_subagent_panel_state.py`
Expected: 全量 passed（`test_mcp_integration.py` 环境性失败除外），ruff 干净。

- [ ] **Step 6: Commit**

```bash
git add lanscoder/app/tui.py lanscoder/app/tui.tcss tests/test_app_tui.py
git commit -m "feat: add x-to-stop subagent panel interaction with arrow selection"
```

---

## 计划自审记录

- **Spec 覆盖**：Part A（取消链路）→ Task 1/2；Part B 状态机 → Task 3；渲染/提示/cancelling/CSS → Task 4；按键/停止/鼠标 → Task 5。spec 第 5 节验收标准逐条落在 Task 的验证步骤。无缺口。
- **占位符扫描**：每步均含完整可执行代码与预期输出，无 TBD/TODO。
- **类型一致性**：`SubagentRow`、`build_rows`、`move_selection`、`can_enter_selection`、`stop_target`、`FG_ID` 在 Task 3 定义，Task 4/5 使用处签名一致；`_subagent_selected` / `_subagent_select_mode` 在 Task 4 定义、Task 5 消费；`cancellation_token` 参数在 Task 1/2 使用，与 `AgentLoop.__init__` 签名一致。
