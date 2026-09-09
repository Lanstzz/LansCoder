"""Acceptance contracts for delayed persistence of the CLI/TUI primary session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lanscoder.agent.session import AgentSession
from lanscoder.app.factory import create_lanscoder_app
from lanscoder.context.store import JsonlSessionStore
from lanscoder.memory.models import MemoryRecord, MemoryScope
from lanscoder.observability.index import JournalTraceIndex
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, ToolCall, ToolDefinition
from lanscoder.session.catalog import SessionCatalog
from lanscoder.storage import LansCoderPaths
from lanscoder.tools.types import Tool, make_text_result
from lanscoder.tools.write import create_write_tool


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeMcpManager:
    def connect_all_in_background(self) -> None:
        return None

    def tools(self) -> tuple[()]:
        return ()

    def close(self) -> None:
        return None


def _app(tmp_path: Path, responses: list[ChatResponse], **kwargs):
    return create_lanscoder_app(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        provider=FakeProvider(responses),
        tools=kwargs.pop("tools", []),
        mcp_manager_factory=lambda _: FakeMcpManager(),
        **kwargs,
    )


def _paths(tmp_path: Path) -> LansCoderPaths:
    return LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)


def _events(tmp_path: Path, session_id: str) -> list:
    return JsonlSessionStore(_paths(tmp_path).storage_root).journal.read_events(session_id)


def _reply(content: str = "done") -> ChatResponse:
    return ChatResponse(provider="fake", model="fake-model", content=content)


def test_cli_tui_runtime_stays_unmaterialized_until_input(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    paths = _paths(tmp_path)
    catalog = SessionCatalog(paths.storage_root, project_id=paths.project_id)
    trace_index = JournalTraceIndex(paths)

    assert not list(paths.sessions.glob("*.jsonl"))
    assert catalog.list_sessions() == []
    assert trace_index.list_summaries() == []

    app.on_unmount()

    assert not list(paths.sessions.glob("*.jsonl"))
    assert catalog.list_sessions() == []
    assert trace_index.list_summaries() == []


def test_first_turn_persists_root_before_trace_and_user_message(tmp_path: Path) -> None:
    app = _app(tmp_path, [_reply()])

    app.chat_runner.run_user_turn("hello")
    events = _events(tmp_path, app.current_session.session_id)

    created = [event for event in events if event.kind == "session.created"]
    assert len(created) == 1
    assert created[0].sequence == 1
    assert created[0].branch_id == created[0].data["root_branch_id"]
    assert [event.kind for event in events].index("session.created") < [event.kind for event in events].index("trace.started")
    assert [event.kind for event in events].index("trace.started") < next(index for index, event in enumerate(events) if event.kind == "message.appended" and event.data.get("role") == "user")


def test_new_command_stages_title_until_first_turn(tmp_path: Path) -> None:
    app = _app(tmp_path, [_reply()])
    result = app.command_handler.handle("/new staged title")
    session_id = app.current_session.session_id
    paths = _paths(tmp_path)

    assert result.handled is True
    assert not paths.session(session_id).exists()
    assert SessionCatalog(paths.storage_root, project_id=paths.project_id).list_sessions() == []

    app.chat_runner.run_user_turn("first input")
    events = _events(tmp_path, session_id)
    assert events[0].kind == "session.created"
    assert events[0].data["title"] == "staged title"
    assert not any(event.kind == "session.metadata_updated" for event in events)


def test_rename_command_stages_title_until_first_turn(tmp_path: Path) -> None:
    app = _app(tmp_path, [_reply()])
    result = app.command_handler.handle("/rename renamed title")
    session_id = app.current_session.session_id
    paths = _paths(tmp_path)

    assert result.handled is True
    assert not paths.session(session_id).exists()
    assert SessionCatalog(paths.storage_root, project_id=paths.project_id).list_sessions() == []
    app.chat_runner.run_user_turn("first input")

    events = _events(tmp_path, session_id)
    assert events[0].data["title"] == "renamed title"
    assert not any(event.kind == "session.metadata_updated" for event in events)


def test_resume_of_provisional_session_does_not_materialize_it(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    session_id = app.current_session.session_id
    result = app.command_handler.handle(f"/resume {session_id}")

    assert result.handled is True
    assert not _paths(tmp_path).session(session_id).exists()


def test_fork_from_provisional_session_does_not_materialize_it(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    session_id = app.current_session.session_id
    result = app.command_handler.handle("/fork")

    assert result.handled is True
    assert not _paths(tmp_path).session(session_id).exists()
    assert not list(_paths(tmp_path).sessions.glob("*.jsonl"))


def test_observe_uses_global_explorer_for_provisional_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    try:
        result = app.command_handler.handle("/observe")

        assert result.action["type"] == "open_observatory"
        assert result.action["url"].endswith("traces")
        assert "session_id" not in result.action["url"]
    finally:
        app.on_unmount()


def test_compact_activates_provisional_runtime_before_persisting(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    session_id = app.current_session.session_id

    result = app.command_handler.handle("/compact")

    assert result.handled is True
    assert "session.created" not in result.output
    assert _paths(tmp_path).session(session_id).exists()


def test_primary_activation_is_idempotent_across_turns(tmp_path: Path) -> None:
    app = _app(tmp_path, [_reply("one"), _reply("two")])
    session_id = app.current_session.session_id

    app.chat_runner.run_user_turn("one")
    app.chat_runner.run_user_turn("two")

    events = _events(tmp_path, session_id)
    assert len([event for event in events if event.kind == "session.created"]) == 1
    assert events[0].sequence == 1
    assert events[0].branch_id == events[0].data["root_branch_id"]


def test_two_provisional_instances_activate_one_persisted_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    first = AgentSession.create_provisional_primary(
        store=store,
        session_id="sess_concurrent_activation",
        session_metadata={"kind": "primary"},
    )
    second = AgentSession.create_provisional_primary(
        store=store,
        session_id="sess_concurrent_activation",
        session_metadata={"kind": "primary"},
    )
    barrier = threading.Barrier(2)
    original_ensure = store.ensure_session_created

    def overlap_root_claim(*, session_id, data):
        barrier.wait(timeout=1)
        return original_ensure(session_id=session_id, data=data)

    monkeypatch.setattr(store, "ensure_session_created", overlap_root_claim)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(session.activate) for session in (first, second)]
        [future.result() for future in futures]

    events = store.journal.read_events(first.session_id)
    created = [event for event in events if event.kind == "session.created"]
    assert len(created) == 1
    assert created[0].sequence == 1
    assert created[0].branch_id == created[0].data["root_branch_id"]
    assert first.writer.branch_context == second.writer.branch_context


def test_memory_remember_activates_and_audits_a_provisional_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    session_id = app.current_session.session_id

    result = app.command_handler.handle("/memory remember build: Run the focused tests")

    assert result.output == "Saved project memory 'build'."
    assert app.current_session.session.memory_manager.get(MemoryScope.PROJECT, "build") is not None
    assert [event.kind for event in _events(tmp_path, session_id)] == ["session.created", "memory.updated"]


def test_memory_forget_activates_and_audits_a_provisional_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    session_id = app.current_session.session_id
    manager = app.current_session.session.memory_manager
    manager.write(
        MemoryScope.USER,
        MemoryRecord(name="style", description="Use concise prose", type="user", body="Use concise prose."),
    )

    result = app.command_handler.handle("/memory forget user:style")

    assert result.output == "Forgot user memory 'style'."
    assert manager.get(MemoryScope.USER, "style") is None
    assert [event.kind for event in _events(tmp_path, session_id)] == ["session.created", "memory.updated"]


def test_memory_list_does_not_materialize_a_provisional_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path, [])
    session_id = app.current_session.session_id

    result = app.command_handler.handle("/memory")

    assert result.handled is True
    assert not _paths(tmp_path).session(session_id).exists()


def test_activation_failure_leaves_no_partial_primary_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, [_reply()])

    def fail_primary_creation(*args, **kwargs):
        raise OSError("session activation failed")

    monkeypatch.setattr(app.current_session.session.store, "ensure_session_created", fail_primary_creation)

    with pytest.raises(OSError, match="session activation failed"):
        app.chat_runner.run_user_turn("activate")

    assert not list(_paths(tmp_path).sessions.glob("*.jsonl"))


def test_permission_pause_and_restart_preserve_one_activated_root(tmp_path: Path) -> None:
    app = _app(
        tmp_path,
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="write-1", name="write", arguments={"path": "out.txt", "content": "ok"})],
            )
        ],
        tools=[create_write_tool(tmp_path)],
    )
    session_id = app.current_session.session_id
    waiting = app.chat_runner.run_user_turn("write")
    request_id = app.chat_runner.last_pending_input.id

    assert waiting.finish_reason == "waiting_for_user_input"
    assert len([event for event in _events(tmp_path, session_id) if event.kind == "session.created"]) == 1

    resumed = _app(
        tmp_path,
        [_reply()],
        session_id=session_id,
        resume_session=True,
        tools=[create_write_tool(tmp_path)],
    )
    assert resumed.chat_runner.resume_with_user_input(request_id, "deny").content == "done"
    assert len([event for event in _events(tmp_path, session_id) if event.kind == "session.created"]) == 1


def test_direct_child_session_creation_remains_immediate(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    child = AgentSession.create(
        store=store,
        session_id="child_1",
        session_metadata={"kind": "subagent", "parent_session_id": "parent_1"},
    )

    events = store.journal.read_events(child.session_id)
    assert child.session_id == "child_1"
    assert [event.kind for event in events] == ["session.created"]
    assert events[0].data["kind"] == "subagent"


def test_background_dispatch_records_the_captured_branch_context(tmp_path: Path) -> None:
    background_tool = Tool(
        ToolDefinition(
            name="view",
            description="local background test tool",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        lambda path: make_text_result("view", f"read {path}"),
    )
    app = _app(
        tmp_path,
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="view-1", name="view", arguments={"path": "README.md", "run_in_background": True})],
            ),
            _reply(),
        ],
        tools=[background_tool],
    )

    app.chat_runner.run_user_turn("read in background")
    events = _events(tmp_path, app.current_session.session_id)
    scheduled = next(event for event in events if event.kind == "background.scheduled")
    root = next(event for event in events if event.kind == "session.created")

    assert scheduled.data["dispatch_branch_context"]["branch_id"] == root.data["root_branch_id"]
    assert scheduled.data["dispatch_branch_context"]["project_id"] == _paths(tmp_path).project_id
