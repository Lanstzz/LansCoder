from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lanscoder.agent.session import AgentSession, create_project_permission_manager
from lanscoder.agent.worktree import Worktree
from lanscoder.agent.subagent_engine import SubagentEngine
from lanscoder.context.store import JsonlSessionStore
from lanscoder.core.runtime import create_agent_loop
from lanscoder.observability import JournalTraceIndex, JournalTraceRecorder, TraceScope, project_trace
from lanscoder.core.runtime import AgentChatRunner, CurrentSessionState
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities, ToolCall
from lanscoder.session.index import SessionIndex
from lanscoder.session.access import SessionAccessError
from lanscoder.session.fork import ForkSessionService
from lanscoder.session.resume import ResumeService
from lanscoder.storage import LansCoderPaths, PayloadStore
from lanscoder.subagent.types import SubagentRequest


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        return self.responses.pop(0)


def _child_factory(provider):
    def factory(*, session, tools, observer, cancellation_token, trace_recorder=None, trace_id=None, trace_scope=None):
        return create_agent_loop(
            session=session,
            provider=provider,
            tools=tools,
            observer=observer,
            cancellation_token=cancellation_token,
            enable_delegate_tool=False,
            trace_recorder=trace_recorder,
            trace_id=trace_id,
            trace_scope=trace_scope,
        )

    return factory


def _engine(tmp_path: Path, provider: FakeProvider, *, project_root: Path | None = None):
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=project_root or tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    parent = AgentSession.create(store=store, session_id="parent_session")
    parent.writer.append_session_metadata_updated(project_id=paths.project_id, kind="primary")
    recorder = JournalTraceRecorder(store.journal, PayloadStore(paths))
    scope = TraceScope("parent_session", parent.writer.branch_context.branch_id)
    parent_trace_id = recorder.start_trace(scope, data={"operation": "agent_turn"})
    engine = SubagentEngine(
        store=store,
        provider=provider,
        tools=[],
        project_root=project_root or tmp_path,
        permission_coordinator=parent.permission_coordinator,
        child_runner_factory=_child_factory(provider),
        trace_recorder=recorder,
        trace_id=parent_trace_id,
        trace_scope=scope,
    )
    return paths, store, recorder, scope, parent_trace_id, engine


def test_inline_delegate_persists_child_journal_trace_and_link(tmp_path: Path) -> None:
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="child done")])
    paths, store, recorder, scope, parent_trace_id, engine = _engine(tmp_path, provider)
    triggering_observation_id = recorder.start_observation(
        parent_trace_id,
        "tool",
        observation_id="obs_delegate",
        scope=scope,
        data={"tool_name": "delegate"},
    )

    with scope.activate(trace_id=parent_trace_id, observation_id=triggering_observation_id):
        result = engine.run(
            SubagentRequest(
                role="researcher",
                task="inspect context",
                parent_session_id="parent_session",
            )
        )

    recorder.end_observation(triggering_observation_id, outcome="succeeded")

    assert result.ok is True
    child_events = store.list_events(result.child_session_id)
    child_view = store.rebuild_session_view(result.child_session_id)
    assert child_events
    assert child_events[0].kind == "session.created"
    assert child_events[0].data["kind"] == "subagent"
    assert child_events[0].data["parent_session_id"] == "parent_session"
    assert child_events[0].data["parent_trace_id"] == parent_trace_id
    assert child_events[0].data["parent_observation_id"] == "obs_delegate"
    assert child_events[0].data["delegate_role"] == "researcher"
    assert child_events[0].data["delegate_task"] == "inspect context"
    assert child_events[0].data["project_id"] == paths.project_id
    assert child_events[0].data["worktree_metadata"] == {}
    assert child_view.metadata["kind"] == "subagent"
    assert child_view.metadata["parent_session_id"] == "parent_session"
    assert child_view.metadata["parent_trace_id"] == parent_trace_id
    assert child_view.metadata["parent_observation_id"] == "obs_delegate"
    assert child_view.metadata["triggering_observation_id"] == "obs_delegate"
    assert child_view.metadata["delegate_role"] == "researcher"
    assert child_view.metadata["delegate_task"] == "inspect context"
    assert child_view.metadata["project_id"] == paths.project_id
    assert child_view.metadata["worktree_metadata"] == {}

    child_trace = next(event.trace_id for event in child_events if event.kind == "trace.started")
    link = next(event for event in child_events if event.kind == "trace.linked")
    assert link.data["parent_trace_id"] == parent_trace_id
    assert link.data["child_trace_id"] == child_trace
    assert link.data["parent_observation_id"] == "obs_delegate"
    assert project_trace(child_events, child_trace).status.value == "completed"
    assert any(event.kind == "message.appended" for event in child_events)
    recorder.end_trace(parent_trace_id, final_output="parent done")
    parent_record = project_trace(store.list_events("parent_session"), parent_trace_id)
    assert any(item["child_trace_id"] == child_trace for item in parent_record.links)


def test_child_creation_fails_closed_without_parent_trace(tmp_path: Path) -> None:
    provider = FakeProvider([])
    _, _, _, _, _, engine = _engine(tmp_path, provider)
    engine.trace_recorder = None
    engine.trace_id = None

    with pytest.raises(SessionAccessError, match="parent_trace_id"):
        engine.create_child_session(
            SubagentRequest(
                role="researcher",
                task="must have identity",
                parent_session_id="parent_session",
            ),
            profile=engine.profile("researcher"),
        )


def test_subagent_id_remains_hidden_from_primary_catalog_but_trace_is_indexed(tmp_path: Path) -> None:
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="child done")])
    _, store, _, _, parent_trace_id, engine = _engine(tmp_path, provider)
    result = engine.run(
        SubagentRequest(
            role="researcher",
            task="inspect context",
            parent_session_id="parent_session",
            parent_trace_id=parent_trace_id,
        )
    )

    from lanscoder.session.catalog import SessionCatalog
    from lanscoder.session.index import SessionIndex

    assert result.child_session_id not in {record.session_id for record in SessionCatalog(store.root).list_sessions()}
    assert result.child_session_id in {record.session_id for record in SessionIndex(store.root).list_records(kind="subagent")}
    assert (store.root / "indexes" / "traces.json").exists()
    child_trace_id = next(event.trace_id for event in store.list_events(result.child_session_id) if event.kind == "trace.started")
    assert child_trace_id in {summary.trace_id for summary in JournalTraceIndex(store.root).list_summaries()}


def test_generated_subagent_is_rejected_by_resume_and_fork_access_policy(tmp_path: Path) -> None:
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="child done")])
    _, store, _, _, parent_trace_id, engine = _engine(tmp_path, provider)
    result = engine.run(
        SubagentRequest(
            role="researcher",
            task="inspect context",
            parent_session_id="parent_session",
            parent_trace_id=parent_trace_id,
        )
    )

    for service in (
        ResumeService(store=store, project_root=tmp_path),
        ForkSessionService(store=store, project_root=tmp_path),
    ):
        operation = service.resume if isinstance(service, ResumeService) else service.fork
        with pytest.raises(SessionAccessError, match="primary"):
            operation(result.child_session_id)


def test_delegate_tool_links_child_to_its_tool_observation(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    parent = AgentSession.create(
        store=store,
        session_id="parent_runtime",
        permission_manager=create_project_permission_manager(tmp_path),
    )
    parent.writer.append_session_metadata_updated(project_id=paths.project_id, kind="primary")
    recorder = JournalTraceRecorder(store.journal, PayloadStore(paths))
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="delegate_call",
                        name="delegate",
                        arguments={"role": "researcher", "task": "inspect context"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="child done"),
            ChatResponse(provider="fake", model="fake-model", content="parent done"),
        ]
    )
    runner = AgentChatRunner(
        current_session=CurrentSessionState(parent),
        provider=provider,
        trace_recorder=recorder,
    )

    response = runner.run_user_turn("delegate this")

    assert response.content == "parent done"
    parent_events = store.list_events(parent.session_id)
    delegate_observation = next(event for event in parent_events if event.kind == "observation.started" and event.data.get("observation_type") == "tool" and event.data.get("tool_name") == "delegate")
    child_ids = [record.session_id for record in SessionIndex(store.root).list_records(kind="subagent")]
    assert len(child_ids) == 1
    child_events = store.list_events(child_ids[0])
    child_view = store.rebuild_session_view(child_ids[0])
    assert child_view.metadata["triggering_observation_id"] == delegate_observation.observation_id
    link = next(event for event in child_events if event.kind == "trace.linked")
    assert link.data["parent_observation_id"] == delegate_observation.observation_id


def test_worktree_child_persists_worktree_metadata_and_trace(tmp_path: Path) -> None:
    provider = FakeProvider([])
    paths, store, _, _, parent_trace_id, engine = _engine(tmp_path, provider)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    worktree = Worktree(
        name="child",
        path=worktree_path,
        branch="fc/subagent/child",
        base_ref="HEAD",
    )
    request = SubagentRequest(
        role="coder",
        task="edit in isolation",
        parent_session_id="parent_session",
        parent_trace_id=parent_trace_id,
        triggering_observation_id="obs_delegate",
        isolate_worktree=True,
    )

    child = engine._create_isolated_child_session(
        request,
        profile=engine.profile("coder"),
        worktree=worktree,
        session_id="child_worktree",
    )
    child_trace_id, _ = engine._start_child_trace(
        child,
        request,
        trace_context=engine._child_trace_context(request),
        worktree_metadata=engine._worktree_metadata(worktree),
    )

    view = store.rebuild_session_view(child.session_id)
    created = store.list_events(child.session_id)[0]
    assert created.kind == "session.created"
    assert created.data["kind"] == "subagent"
    assert created.data["parent_session_id"] == "parent_session"
    assert created.data["parent_trace_id"] == parent_trace_id
    assert created.data["parent_observation_id"] == "obs_delegate"
    assert created.data["delegate_role"] == "coder"
    assert created.data["delegate_task"] == "edit in isolation"
    assert created.data["project_id"] == paths.project_id
    assert created.data["worktree_metadata"]["path"] == str(worktree_path)
    assert view.metadata["kind"] == "subagent"
    assert view.metadata["worktree_metadata"] == {
        "isolated": True,
        "path": str(worktree_path),
        "branch": "fc/subagent/child",
    }
    assert view.metadata["worktree_path"] == str(worktree_path)
    assert view.metadata["worktree_branch"] == "fc/subagent/child"
    assert child_trace_id is not None
    assert any(event.kind == "trace.linked" for event in store.list_events(child.session_id))
