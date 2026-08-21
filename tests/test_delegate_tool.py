from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from lanscoder.app.runtime import create_agent_loop
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.session import AgentSession
from lanscoder.agent.subagent_engine import SubagentEngine
from lanscoder.subagent.types import SubagentRequest, SubagentResult
from lanscoder.context.identity import new_session_id
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ProviderCapabilities,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from lanscoder.tools.types import Tool, ToolResult, make_text_result

_ENGINE_HOST_STORE = JsonlSessionStore(Path(tempfile.mkdtemp()))


def _engine_coordinator(*, permission_manager=None):
    """Build a PermissionCoordinator for a standalone SubagentEngine.

    The engine now takes the parent session's ``permission_coordinator``; these
    tests build engines without a parent session, so a host session on a shared
    throwaway store supplies one.  The engine never touches the coordinator's
    session-backed pending/preflight state, so the throwaway host is inert.
    """

    host = AgentSession.create(
        store=_ENGINE_HOST_STORE,
        session_id=new_session_id(),
        agents_md="",
        tools=[],
        permission_manager=permission_manager,
    )
    return host.permission_coordinator


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content='{"decision":"uncertain","basis_message_id":"msg"}',
            )
        self.requests.append(request)
        return self.responses.pop(0)


def _tool(name: str) -> Tool:
    def execute(text: str = "") -> ToolResult:
        return make_text_result(name, f"{name}:{text}")

    return Tool(
        definition=ToolDefinition(
            name=name,
            description=f"tool {name}",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        executor=execute,
    )


def _delegate_call(call_id: str, *, role: str, task: str, **extra) -> ToolCall:
    arguments = {"role": role, "task": task, **extra}
    return ToolCall(id=call_id, name="delegate", arguments=arguments)


def _create_task_plan(session: AgentSession, *, task_id: str) -> None:
    result = session.tool_registry.execute(
        "task_create",
        {
            "mode": "linear",
            "expected_revision": 0,
            "tasks": [
                {
                    "id": task_id,
                    "content": "Run delegated work",
                    "status": "in_progress",
                }
            ],
        },
    )
    assert result.ok is True


def _child_runner_factory(provider):
    """Build a child-loop factory closed over ``provider`` for one test.

    Mirrors the production closure in ``app/runtime.create_agent_loop``: the
    child never gets the delegate tool, and cancellation flows from the parent
    turn through the current cancellation token.
    """

    def _factory(*, session, tools, observer, cancellation_token):
        return create_agent_loop(
            session=session,
            provider=provider,
            tools=tools,
            observer=observer,
            cancellation_token=cancellation_token,
            enable_delegate_tool=False,
        )

    return _factory


def test_subagent_runner_filters_tools_by_profile(tmp_path) -> None:
    provider = FakeProvider([])
    runner = SubagentEngine(
        store=JsonlSessionStore(tmp_path),
        provider=provider,
        tools=[
            _tool("view"),
            _tool("grep"),
            _tool("write"),
            _tool("delegate"),
            _tool("shell"),
        ],
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )

    assert [tool.name for tool in runner.tools_for_role("reviewer")] == ["view", "grep"]
    assert "delegate" not in [tool.name for tool in runner.tools_for_role("coder")]
    assert "write" in [tool.name for tool in runner.tools_for_role("coder")]
    assert "write" not in [tool.name for tool in runner.tools_for_role("researcher")]


def test_child_session_is_metadata_tagged(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )

    child = runner.create_child_session(
        SubagentRequest(
            role="researcher",
            task="inspect context",
            parent_session_id="parent_1",
            path_hints=["lanscoder/agent"],
        ),
        profile=runner.profile("researcher"),
    )

    view = store.rebuild_session_view(child.session_id)
    assert view.metadata["parent_session_id"] == "parent_1"
    assert view.metadata["delegate_role"] == "researcher"
    assert view.metadata["delegate_task"] == "inspect context"


def test_subagent_run_restricts_child_tools_and_deletes_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="child done")])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view"), _tool("delegate")],
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )

    result = runner.run(
        SubagentRequest(
            role="researcher",
            task="inspect context",
            parent_session_id="parent_1",
            path_hints=["lanscoder/agent"],
        )
    )

    assert result.ok is True
    assert result.summary == "child done"
    # The child request excludes delegate and includes the profile's tools.
    assert "delegate" not in [definition.name for definition in provider.requests[0].tools]
    assert "view" in [definition.name for definition in provider.requests[0].tools]
    # The finished child session is removed from disk and the resume index.
    assert not store._session_path(result.child_session_id).exists()
    from lanscoder.session.index import SessionIndex

    records = SessionIndex(tmp_path).list_records()
    assert all(record.session_id != result.child_session_id for record in records)


def test_agent_loop_registers_delegate_and_foreground_returns_summary(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="child summary")])
    session = AgentSession.create(store=store, session_id="parent_delegate", tools=[_tool("view")])
    make_loop(session=session, provider=provider)

    assert "delegate" in session.tool_registry.names()
    result = session.tool_registry.execute("delegate", {"role": "researcher", "task": "read docs"})

    assert result.ok is True
    assert result.data["role"] == "researcher"
    assert result.data["child_session_id"]
    assert "child summary" in result.content


def test_foreground_delegate_result_includes_usage_and_elapsed(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="child summary",
                usage=TokenUsage(input_tokens=10, output_tokens=20, total_tokens=2500),
            )
        ]
    )
    session = AgentSession.create(store=store, session_id="parent_usage", tools=[_tool("view")])
    make_loop(session=session, provider=provider)

    result = session.tool_registry.execute("delegate", {"role": "researcher", "task": "read docs"})

    assert result.ok is True
    assert result.data["total_tokens"] == 2500
    assert result.data["provider_calls"] == 1
    assert result.data["elapsed_seconds"] is not None
    assert "2.5k tokens" in result.content
    assert "calls" in result.content


def test_foreground_progress_writes_to_runner_tracker(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )
    tracker = {
        "label": "researcher",
        "started_at": 0.0,
        "provider_calls": 0,
        "total_tokens": 0,
    }

    observer = runner._make_child_observer(tracker)
    observer.on_progress(2, 1500)

    assert tracker["provider_calls"] == 2
    assert tracker["total_tokens"] == 1500


def test_background_delegate_does_not_expose_foreground_tracker(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    observed: list[dict | None] = []
    runner: SubagentEngine

    class ProbeProvider(FakeProvider):
        def complete(self, request):
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            observed.append(runner.foreground_progress)
            return ChatResponse(provider="fake", model="fake-model", content="child done")

    manager = BackgroundJobManager()
    provider = ProbeProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        background_manager=manager,
        child_runner_factory=_child_runner_factory(provider),
    )

    def job_func() -> ToolResult:
        runner.run(SubagentRequest(role="researcher", task="inspect", parent_session_id="parent_1"))
        return make_text_result("delegate", "done")

    job = manager.start(job_func, tool_name="delegate")
    try:
        assert manager.wait(timeout=5) is True
    finally:
        manager.shutdown()

    assert observed, "child provider must run as part of the background job"
    assert observed[0] is None  # no phantom foreground tracker inside a background delegate
    assert job.progress, "background progress must go to the job, not a foreground tracker"


def test_foreground_delegate_survives_background_delegate_finish(tmp_path) -> None:
    """Regression: a background delegate completing while a foreground delegate is
    mid-run must not crash the foreground subagent ('NoneType' object has no
    attribute 'update') nor wipe its progress tracker."""

    fg_started = threading.Event()
    release_fg = threading.Event()

    class GatedProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            # 第一个非 certainty 调用必然属于前台子 agent：前台先启动并在此阻塞，
            # 后台 job 直到 fg_started 置位后才开始。
            if self._gate_active:
                self._gate_active = False
                fg_started.set()
                if not release_fg.wait(5):
                    raise AssertionError("foreground release gate was not opened")
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content="child done",
                usage=TokenUsage(input_tokens=5, output_tokens=5, total_tokens=100),
            )

    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    provider = GatedProvider([])
    provider._gate_active = True
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        background_manager=manager,
        child_runner_factory=_child_runner_factory(provider),
    )
    foreground_results: list[SubagentResult] = []

    def fg_func() -> None:
        foreground_results.append(runner.run(SubagentRequest(role="researcher", task="foreground task", parent_session_id="p_fg")))

    fg_thread = threading.Thread(target=fg_func)
    fg_thread.start()
    assert fg_started.wait(5), "foreground delegate should start and block on its provider"

    def bg_func() -> ToolResult:
        runner.run(SubagentRequest(role="researcher", task="background task", parent_session_id="p_bg"))
        return make_text_result("delegate", "done")

    try:
        manager.start(bg_func, tool_name="delegate")
        assert manager.wait(timeout=5) is True
    finally:
        manager.shutdown()

    release_fg.set()
    fg_thread.join(10)
    assert not fg_thread.is_alive(), "foreground delegate should complete after release"
    assert foreground_results and foreground_results[0].ok is True
    assert "Subagent failed" not in foreground_results[0].summary


def test_foreground_delegate_cancel_aborts_child(tmp_path) -> None:
    """Foreground subagent must abort with AgentCancelledError when the parent turn
    is cancelled — it must not run to completion and be mislabelled as failed."""

    from lanscoder.utils.cancellation import (
        AgentCancelledError,
        CancellationToken,
        cancellation_context,
    )

    store = JsonlSessionStore(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="child done")

    provider = BlockingProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
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


def test_background_delegate_cancel_aborts_child(tmp_path) -> None:
    """A cancelled background job must abort its inline subagent with
    AgentCancelledError and finish cancelled, not run to completion."""

    from lanscoder.utils.cancellation import AgentCancelledError

    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="child done")

    provider = BlockingProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        background_manager=manager,
        child_runner_factory=_child_runner_factory(provider),
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


def test_background_delegate_cancel_keeps_job_error_clear(tmp_path) -> None:
    """A cancelled background job must not be framed as a failure.

    When the child subagent raises AgentCancelledError straight out of the job
    function (nothing swallows it), the production BackgroundJobManager._run
    path must finish the job as cancelled with no error — never
    '后台任务执行失败：Agent turn was interrupted.'
    """

    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="child done")

    provider = BlockingProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        background_manager=manager,
        child_runner_factory=_child_runner_factory(provider),
    )

    def job_func() -> ToolResult:
        # Deliberately do NOT catch AgentCancelledError: the production _run
        # path must treat it as an interrupt, not a failure.
        runner.run(
            SubagentRequest(
                role="researcher",
                task="background task",
                parent_session_id="p_bg",
            )
        )
        return make_text_result("delegate", "done")

    try:
        job = manager.start(job_func, tool_name="delegate")
        assert started.wait(5), "background child should start and block on its provider"
        manager.cancel(job.id)
        release.set()
        assert manager.wait(timeout=5) is True
    finally:
        manager.shutdown()

    assert job.status == "cancelled"
    assert job.error is None


def test_background_delegate_returns_placeholder_and_notification(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    gate = threading.Event()

    def child_response() -> ChatResponse:
        gate.wait(5)
        return ChatResponse(provider="fake", model="fake-model", content="background child done")

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            self.requests.append(request)
            return child_response()

    provider = BlockingProvider([])
    try:
        session = AgentSession.create(store=store, session_id="parent_bg_delegate", tools=[_tool("view")])
        loop = make_loop(session=session, provider=provider, background_manager=manager)
        _create_task_plan(session, task_id="research_a")
        session.append_user_message("start")
        call = _delegate_call(
            "call_delegate",
            role="researcher",
            task="slow research",
            run_in_background=True,
            task_id="research_a",
        )
        session.append_assistant_response(
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[call],
                finish_reason="tool_calls",
            )
        )

        state = loop.tool_executor.execute_interactive([call])
        assert state.pending_input is None
        tool_result = [message for message in session.rebuild_view().messages if message.role == "tool"][0].parts[0]
        assert tool_result.metadata["data"]["background_job_id"] == "bg_0001"
        assert tool_result.metadata["data"]["task_id"] == "research_a"
        assert tool_result.metadata["data"]["observed_revision"] == 1

        gate.set()
        assert manager.wait(timeout=5) is True
        loop._append_background_notifications()
        notifications = [message.parts[0].content for message in session.rebuild_view().messages if message.role == "notification" and "<task_notification>" in message.parts[0].content]
        assert len(notifications) == 1
        assert "<task_id>research_a</task_id>" in notifications[0]
        assert "background child done" in notifications[0]
        plan = session.rebuild_view().task_plan
        assert plan is not None
        assert plan.revision == 2
        assert plan.tasks[0].status == "completed"
    finally:
        gate.set()
        manager.wait(timeout=5)
        manager.shutdown()


def test_coder_delegate_background_rejected_without_git_repo(tmp_path, make_loop) -> None:
    """Background coder needs worktree isolation; a non-git project must be refused.

    Phase 4 allows background coder only when a git worktree can be created.  With
    no permission manager / non-git root, isolation is unavailable, so the call is
    rejected up front with ``worktree_unavailable`` and no job is started.
    """

    store = JsonlSessionStore(tmp_path)
    manager = BackgroundJobManager()
    provider = FakeProvider([])
    try:
        session = AgentSession.create(store=store, session_id="parent_coder_bg", tools=[_tool("view")])
        loop = make_loop(session=session, provider=provider, background_manager=manager)
        session.append_user_message("start")
        call = _delegate_call("call_delegate", role="coder", task="edit files", run_in_background=True)
        session.append_assistant_response(
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[call],
                finish_reason="tool_calls",
            )
        )

        loop.tool_executor.execute_interactive([call])
        tool_result = [message for message in session.rebuild_view().messages if message.role == "tool"][0].parts[0]
        assert tool_result.metadata["ok"] is False
        assert tool_result.metadata["data"]["background_rejected"] == "worktree_unavailable"
        assert manager.list() == []
    finally:
        manager.shutdown()


def _init_git_repo(root) -> None:
    import subprocess

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.co")
    _git("config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-qm", "init")


def _write_call(call_id: str, path: str, content: str) -> ToolCall:
    return ToolCall(id=call_id, name="write", arguments={"path": path, "content": content})


def test_isolated_coder_writes_only_in_worktree(tmp_path) -> None:
    """Phase 4: a worktree-isolated coder mutates the worktree, never the parent tree."""

    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode
    from lanscoder.agent.subagent_engine import SubagentEngine
    from lanscoder.subagent.types import SubagentRequest

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Dirty parent proves isolation does not depend on a clean parent tree.
    (repo / "seed.txt").write_text("seed dirty\n", encoding="utf-8")

    write_call = _write_call("w1", "newfile.py", "print('hi')\n")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[write_call],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="Implemented newfile.py"),
        ]
    )
    store = JsonlSessionStore(repo / ".fc_sessions")
    permission_manager = PermissionManager(policy=DefaultPermissionPolicy(repo), mode=PermissionMode.STANDARD)
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[],
        project_root=repo,
        permission_coordinator=_engine_coordinator(permission_manager=permission_manager),
        child_runner_factory=_child_runner_factory(provider),
    )

    result = runner.run(
        SubagentRequest(
            role="coder",
            task="create newfile.py",
            parent_session_id="p1",
            isolate_worktree=True,
        )
    )

    assert result.ok is True
    assert result.worktree_branch == f"fc/subagent/{result.child_session_id}"
    assert result.worktree_path is not None
    assert "newfile.py" in result.files_changed
    assert "newfile.py" in (result.diff_summary or "")
    # 隔离目录里有新文件，父工作区没有；父的 dirty 文件也没被动过。
    assert (Path(result.worktree_path) / "newfile.py").exists()
    assert not (repo / "newfile.py").exists()
    assert (repo / "seed.txt").read_text(encoding="utf-8").strip() == "seed dirty"
    # 子会话在完成后被删除，不会出现在 /resume 里。
    assert not store._session_path(result.child_session_id).exists()


def test_isolated_coder_can_delete_inside_worktree_without_parent_delete(
    tmp_path,
) -> None:
    """DELETE_PATH is allowed only in the isolated worktree, never in the parent tree."""

    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode
    from lanscoder.agent.subagent_engine import SubagentEngine
    from lanscoder.subagent.types import SubagentRequest

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    delete_call = ToolCall(id="d1", name="delete", arguments={"path": "seed.txt"})
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[delete_call],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="Deleted seed.txt"),
        ]
    )
    runner = SubagentEngine(
        store=JsonlSessionStore(repo / ".fc_sessions"),
        provider=provider,
        tools=[],
        project_root=repo,
        permission_coordinator=_engine_coordinator(permission_manager=PermissionManager(policy=DefaultPermissionPolicy(repo), mode=PermissionMode.STANDARD)),
        child_runner_factory=_child_runner_factory(provider),
    )

    result = runner.run(
        SubagentRequest(
            role="coder",
            task="delete seed.txt in isolation",
            parent_session_id="p1",
            isolate_worktree=True,
        )
    )

    assert result.ok is True
    assert "seed.txt" in result.files_changed
    assert result.worktree_path is not None
    assert not (Path(result.worktree_path) / "seed.txt").exists()
    assert (repo / "seed.txt").exists()


def test_isolated_coder_dangerous_shell_is_denied_not_waiting(tmp_path) -> None:
    """A dangerous shell in a background coder is auto-DENIED (not paused), so the
    child keeps running instead of surfacing waiting_for_user_input."""

    from lanscoder.agent.subagent_engine import SubagentEngine
    from lanscoder.subagent.types import SubagentRequest

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    shell_call = ToolCall(id="s1", name="shell", arguments={"command": "rm seed.txt"})
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[shell_call],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="rm was denied; leaving the file.",
            ),
        ]
    )
    runner = SubagentEngine(
        store=JsonlSessionStore(repo / ".fc_sessions"),
        provider=provider,
        tools=[],
        project_root=repo,
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )

    result = runner.run(
        SubagentRequest(
            role="coder",
            task="try dangerous shell",
            parent_session_id="p1",
            isolate_worktree=True,
        )
    )

    assert result.ok is True
    assert result.error is None
    assert result.worktree_path is not None
    assert (Path(result.worktree_path) / "seed.txt").exists()
    assert (repo / "seed.txt").exists()


def test_background_child_permission_manager_is_autonomous(tmp_path) -> None:
    """Background non-worktree subagents get an autonomous AGGRESSIVE manager so
    they never pause for interactive confirmation."""

    from lanscoder.permissions.types import PermissionMode

    repo = tmp_path / "repo"
    repo.mkdir()
    provider = FakeProvider([])
    runner = SubagentEngine(
        store=JsonlSessionStore(repo / ".fc_sessions"),
        provider=provider,
        tools=[],
        project_root=repo,
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )

    manager = runner.permission_coordinator.child_permission_manager(
        root=repo,
        mutation=False,
        background=True,
    )

    assert manager is not None
    assert manager.autonomous is True
    assert manager.mode == PermissionMode.AGGRESSIVE


def test_background_coder_uses_worktree_and_leaves_parent_untouched(tmp_path, make_loop) -> None:
    """Phase 4: background delegate for coder runs isolated and reports a diff summary."""

    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    write_call = _write_call("w1", "bg_new.py", "x = 1\n")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[write_call],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="coder done"),
        ]
    )
    store = JsonlSessionStore(repo / ".fc_sessions")
    permission_manager = PermissionManager(policy=DefaultPermissionPolicy(repo), mode=PermissionMode.STANDARD)
    manager = BackgroundJobManager()
    try:
        session = AgentSession.create(
            store=store,
            session_id="parent_bg_coder",
            permission_manager=permission_manager,
        )
        loop = make_loop(session=session, provider=provider, background_manager=manager)
        _create_task_plan(session, task_id="impl")
        session.append_user_message("start")
        call = _delegate_call(
            "call_delegate",
            role="coder",
            task="create bg_new.py",
            run_in_background=True,
            task_id="impl",
        )
        session.append_assistant_response(
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[call],
                finish_reason="tool_calls",
            )
        )

        loop.tool_executor.execute_interactive([call])
        tool_result = [message for message in session.rebuild_view().messages if message.role == "tool"][0].parts[0]
        assert tool_result.metadata["ok"] is True
        assert tool_result.metadata["data"]["background_job_id"] == "bg_0001"
        assert tool_result.metadata["data"].get("background_rejected") is None

        assert manager.wait(timeout=10) is True
        loop._append_background_notifications()
        notifications = [message.parts[0].content for message in session.rebuild_view().messages if message.role == "notification" and "<task_notification>" in message.parts[0].content]
        assert len(notifications) == 1
        assert "bg_new.py" in notifications[0]
        assert "<task_id>impl</task_id>" in notifications[0]
        # 后台 coder 的改动只在隔离 worktree，父工作区看不到。
        assert not (repo / "bg_new.py").exists()
    finally:
        manager.shutdown()


def test_isolated_coder_cancel_aborts_child(tmp_path) -> None:
    """A cancelled background coder must abort its worktree-isolated subagent with
    AgentCancelledError and finish cancelled, not run to completion."""

    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode
    from lanscoder.utils.cancellation import AgentCancelledError

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    manager = BackgroundJobManager()
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
                return super().complete(request)
            started.set()
            if not release.wait(5):
                raise AssertionError("release gate was not opened")
            return ChatResponse(provider="fake", model="fake-model", content="coder done")

    store = JsonlSessionStore(repo / ".fc_sessions")
    provider = BlockingProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[],
        project_root=repo,
        permission_coordinator=_engine_coordinator(permission_manager=PermissionManager(policy=DefaultPermissionPolicy(repo), mode=PermissionMode.STANDARD)),
        background_manager=manager,
        child_runner_factory=_child_runner_factory(provider),
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


def test_isolated_coder_without_git_repo_returns_error(tmp_path) -> None:
    """When isolation is requested but the project is not a git repo, fail cleanly."""

    from lanscoder.agent.subagent_engine import SubagentEngine
    from lanscoder.subagent.types import SubagentRequest

    provider = FakeProvider([])
    runner = SubagentEngine(
        store=JsonlSessionStore(tmp_path),
        provider=provider,
        tools=[],
        project_root=tmp_path,
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )
    result = runner.run(
        SubagentRequest(
            role="coder",
            task="edit",
            parent_session_id="p1",
            isolate_worktree=True,
        )
    )
    assert result.ok is False
    assert result.error == "worktree_unavailable"
    # 没有真正调用 provider（在创建 worktree 前就返回了）。
    assert provider.requests == []


def test_child_factory_loop_carries_tight_guardrail_limits(tmp_path) -> None:
    """A child loop built the way the assembly root builds one carries the tight budget.

    Regression for the human decision on the child delegate budget: the child
    factory must forward ``DEFAULT_CHILD_LIMITS`` (20 tool rounds / 40 provider
    calls / 600s), not the parent runtime's looser limits.
    """

    from lanscoder.agent.loop_limits import AgentLoopLimits
    from lanscoder.agent.subagent_engine import DEFAULT_CHILD_LIMITS
    from lanscoder.providers.types import MainRequestOptions

    assert DEFAULT_CHILD_LIMITS.max_tool_rounds == 20
    assert DEFAULT_CHILD_LIMITS.max_provider_calls == 40
    assert DEFAULT_CHILD_LIMITS.max_turn_seconds == 600

    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        limits=DEFAULT_CHILD_LIMITS,
        request_options=MainRequestOptions(),
        child_runner_factory=lambda **kwargs: None,
    )
    child_session = runner.create_child_session(
        SubagentRequest(role="researcher", task="inspect", parent_session_id="p_budget"),
        profile=runner.profile("researcher"),
    )
    try:
        child_loop = create_agent_loop(
            session=child_session,
            provider=provider,
            tools=[_tool("view")],
            cancellation_token=None,
            background_manager=None,
            enable_delegate_tool=False,
            limits=DEFAULT_CHILD_LIMITS,
            request_options=MainRequestOptions(),
        )
        assert child_loop.limits.max_tool_rounds == 20
        assert child_loop.limits.max_provider_calls == 40
        assert child_loop.limits.max_turn_seconds == 600
        assert isinstance(child_loop.limits, AgentLoopLimits)
    finally:
        runner._delete_child_session(child_session.session_id)


def test_child_delegate_does_not_inherit_parent_guardrail_limits(make_loop, tmp_path) -> None:
    """Delegate children keep the tight child budget, not the parent's limits.

    The parent is configured with ``max_provider_calls=1``; a child that needed
    2 provider calls would stop after the first if it inherited that budget. It
    completes both calls, proving the production child factory forwards the tight
    child default, not the parent's limits.
    """

    from lanscoder.agent.loop_limits import AgentLoopLimits
    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode

    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="parent_tight_budget",
        tools=[_tool("view")],
        permission_manager=PermissionManager(policy=DefaultPermissionPolicy(tmp_path), mode=PermissionMode.BYPASS),
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="c1", name="view", arguments={"text": "a"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="child done"),
        ]
    )
    make_loop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_provider_calls=1, max_tool_rounds=5, max_turn_seconds=60),
    )

    result = session.tool_registry.execute("delegate", {"role": "researcher", "task": "read docs"})

    assert result.ok is True
    # The child needed 2 provider calls; inheriting the parent's max_provider_calls=1
    # would have stopped it after the first with a provider-call-limit response.
    assert len(provider.requests) == 2


def test_child_failure_message_surfaces_to_parent(tmp_path) -> None:
    """A crashing child surfaces its concrete error message in the SubagentResult.

    Regression for the whole-branch finding: generic child failures must reach
    the parent model again (base surfaced ``str(exc)``), not a generic
    "child_loop_failed" summary with only a log trace.
    """

    store = JsonlSessionStore(tmp_path)

    class ExplodingProvider(FakeProvider):
        def complete(self, request):
            raise RuntimeError("child exploded")

    provider = ExplodingProvider([])
    runner = SubagentEngine(
        store=store,
        provider=provider,
        tools=[_tool("view")],
        permission_coordinator=_engine_coordinator(),
        child_runner_factory=_child_runner_factory(provider),
    )

    result = runner.run(SubagentRequest(role="researcher", task="inspect", parent_session_id="p1"))

    assert result.ok is False
    assert result.error == "child exploded"
    assert "child exploded" in result.summary
