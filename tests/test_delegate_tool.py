from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.loop import AgentLoop
from lanscoder.agent.session import AgentSession
from lanscoder.agent.subagent import SubagentRunner
from lanscoder.subagent.types import SubagentRequest
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities, ToolCall, ToolDefinition
from lanscoder.tools.types import Tool, ToolResult, make_text_result


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
            return ChatResponse(provider="fake", model="fake-model", content='{"decision":"uncertain","basis_message_id":"msg"}')
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
            "tasks": [{"id": task_id, "content": "Run delegated work", "status": "in_progress"}],
        },
    )
    assert result.ok is True


def test_subagent_runner_filters_tools_by_profile(tmp_path) -> None:
    provider = FakeProvider([])
    runner = SubagentRunner(
        store=JsonlSessionStore(tmp_path),
        provider=provider,
        tools=[_tool("view"), _tool("grep"), _tool("write"), _tool("delegate"), _tool("shell")],
    )

    assert [tool.name for tool in runner.tools_for_role("reviewer")] == ["view", "grep"]
    assert "delegate" not in [tool.name for tool in runner.tools_for_role("coder")]
    assert "write" in [tool.name for tool in runner.tools_for_role("coder")]
    assert "write" not in [tool.name for tool in runner.tools_for_role("researcher")]


def test_child_session_is_metadata_tagged(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    runner = SubagentRunner(store=store, provider=FakeProvider([]), tools=[_tool("view")])

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
    runner = SubagentRunner(store=store, provider=provider, tools=[_tool("view"), _tool("delegate")])

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


def test_agent_loop_registers_delegate_and_foreground_returns_summary(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="child summary")])
    session = AgentSession.create(store=store, session_id="parent_delegate", tools=[_tool("view")])
    AgentLoop(session=session, provider=provider)

    assert "delegate" in session.tool_registry.names()
    result = session.tool_registry.execute("delegate", {"role": "researcher", "task": "read docs"})

    assert result.ok is True
    assert result.data["role"] == "researcher"
    assert result.data["child_session_id"]
    assert "child summary" in result.content


def test_background_delegate_returns_placeholder_and_notification(tmp_path) -> None:
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
        loop = AgentLoop(session=session, provider=provider, background_manager=manager)
        _create_task_plan(session, task_id="research_a")
        session.append_user_message("start")
        call = _delegate_call(
            "call_delegate",
            role="researcher",
            task="slow research",
            run_in_background=True,
            task_id="research_a",
        )
        session.append_assistant_response(ChatResponse(provider="fake", model="fake-model", content="", tool_calls=[call], finish_reason="tool_calls"))

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


def test_coder_delegate_background_rejected_without_git_repo(tmp_path) -> None:
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
        loop = AgentLoop(session=session, provider=provider, background_manager=manager)
        session.append_user_message("start")
        call = _delegate_call("call_delegate", role="coder", task="edit files", run_in_background=True)
        session.append_assistant_response(ChatResponse(provider="fake", model="fake-model", content="", tool_calls=[call], finish_reason="tool_calls"))

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
    from lanscoder.agent.subagent import SubagentRunner
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
    runner = SubagentRunner(
        store=store,
        provider=provider,
        tools=[],
        project_root=repo,
        permission_manager=permission_manager,
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


def test_isolated_coder_can_delete_inside_worktree_without_parent_delete(tmp_path) -> None:
    """DELETE_PATH is allowed only in the isolated worktree, never in the parent tree."""

    from lanscoder.permissions.manager import PermissionManager
    from lanscoder.permissions.policy import DefaultPermissionPolicy
    from lanscoder.permissions.types import PermissionMode
    from lanscoder.agent.subagent import SubagentRunner
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
    runner = SubagentRunner(
        store=JsonlSessionStore(repo / ".fc_sessions"),
        provider=provider,
        tools=[],
        project_root=repo,
        permission_manager=PermissionManager(policy=DefaultPermissionPolicy(repo), mode=PermissionMode.STANDARD),
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

    from lanscoder.agent.subagent import SubagentRunner
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
            ChatResponse(provider="fake", model="fake-model", content="rm was denied; leaving the file."),
        ]
    )
    runner = SubagentRunner(
        store=JsonlSessionStore(repo / ".fc_sessions"),
        provider=provider,
        tools=[],
        project_root=repo,
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
    runner = SubagentRunner(
        store=JsonlSessionStore(repo / ".fc_sessions"),
        provider=FakeProvider([]),
        tools=[],
        project_root=repo,
    )

    manager = runner._background_child_permission_manager()

    assert manager is not None
    assert manager.autonomous is True
    assert manager.mode == PermissionMode.AGGRESSIVE


def test_background_coder_uses_worktree_and_leaves_parent_untouched(tmp_path) -> None:
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
        session = AgentSession.create(store=store, session_id="parent_bg_coder", permission_manager=permission_manager)
        loop = AgentLoop(session=session, provider=provider, background_manager=manager)
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


def test_isolated_coder_without_git_repo_returns_error(tmp_path) -> None:
    """When isolation is requested but the project is not a git repo, fail cleanly."""

    from lanscoder.agent.subagent import SubagentRunner
    from lanscoder.subagent.types import SubagentRequest

    provider = FakeProvider([])
    runner = SubagentRunner(
        store=JsonlSessionStore(tmp_path),
        provider=provider,
        tools=[],
        project_root=tmp_path,
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
