from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import lanscoder.cli as cli
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.subagent_engine import SubagentEngine
from lanscoder.context.llm_compact import LlmCompactRequest, LlmCompactService
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.core.runtime import create_agent_loop
from lanscoder.core.session import create_agent_session
from lanscoder.app.factory import create_lanscoder_app
from lanscoder.journal import JournalCorruptError, JournalStore
from lanscoder.observability import JournalTraceRecorder, TraceScope, project_trace
from lanscoder.observability.web.api import ObservatoryQueryService, ObservatoryProblem
from lanscoder.providers.anthropic_provider import AnthropicProvider
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.openai_compatible import OpenAICompatibleProvider
from lanscoder.providers.types import ChatMessage, ChatRequest, ChatResponse, TokenUsage, ToolCall
from lanscoder.session.access import SessionAccessError, SessionAccessPolicy, project_id_for_path
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.fork import ForkSessionService
from lanscoder.session.resume import ResumeService
from lanscoder.storage import LansCoderPaths, PayloadStore
from lanscoder.subagent.types import SubagentRequest
from lanscoder.tools.types import make_text_result
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


def _response(content: str, *, usage: TokenUsage | None = None, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        provider="fake",
        model="fake-model",
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=usage or TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
        raw={"content": content},
    )


def _child_runner_factory(provider: FakeProvider):
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


def test_fake_provider_cross_project_trace_generation_permission_child_background_and_web_views(tmp_path: Path) -> None:
    storage_root = tmp_path / "runtime"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    background = BackgroundJobManager(max_workers=1)
    try:
        parent_provider = FakeProvider(
            [
                _response(
                    "",
                    tool_calls=[ToolCall(id="call_write", name="write", arguments={"path": "result.txt", "content": "written"})],
                ),
                _response("permission resumed", usage=TokenUsage(input_tokens=8, output_tokens=4, total_tokens=12, usage_details={"cached": 2})),
            ]
        )
        parent = create_agent_session(
            provider=parent_provider,
            project_root=project_a,
            storage_root=storage_root,
            session_id="sess_primary_a",
            tools=[create_write_tool(project_a)],
            background_manager=background,
        )
        other_provider = FakeProvider([_response("other project")])
        other = create_agent_session(
            provider=other_provider,
            project_root=project_b,
            storage_root=storage_root,
            session_id="sess_primary_b",
            tools=[],
        )
        other_response = other.runner.run_user_turn("other project")
        assert other_response.content == "other project"

        waiting = parent.runner.run_user_turn("write the result")
        assert waiting.finish_reason == "waiting_for_user_input"
        pending = parent.runner.last_pending_input
        assert pending is not None
        resumed = parent.runner.resume_with_user_input(pending.id, "allow_once")
        assert resumed.content == "permission resumed"
        assert (project_a / "result.txt").read_text(encoding="utf-8") == "written"

        events = parent.session.store.list_events(parent.session.session_id)
        parent_trace = next(event.trace_id for event in events if event.kind == "trace.started")
        assert any(event.kind == "trace.paused" and event.trace_id == parent_trace for event in events)
        assert any(event.kind == "trace.resumed" and event.trace_id == parent_trace for event in events)
        parent_branch = parent.session.writer.branch_context
        assert parent_branch is not None
        recorder = parent.runner.trace_recorder
        assert isinstance(recorder, JournalTraceRecorder)
        parent_scope = TraceScope(parent.session.session_id, parent_branch.branch_id)
        parent_observation = recorder.start_observation(
            parent_trace,
            "tool",
            scope=parent_scope,
            data={"tool_name": "delegate", "tool_call_id": "call_delegate"},
        )

        child_provider = FakeProvider([_response("child result")])
        child_engine = SubagentEngine(
            store=parent.session.store,
            provider=child_provider,
            tools=[],
            project_root=project_a,
            permission_coordinator=parent.session.permission_coordinator,
            child_runner_factory=_child_runner_factory(child_provider),
            trace_recorder=recorder,
            trace_id=parent_trace,
            trace_scope=parent_scope,
        )
        with parent_scope.activate(trace_id=parent_trace, observation_id=parent_observation):
            child_result = child_engine.run(
                SubagentRequest(
                    role="researcher",
                    task="inspect the result",
                    parent_session_id=parent.session.session_id,
                    parent_trace_id=parent_trace,
                    triggering_observation_id=parent_observation,
                )
            )
        recorder.end_observation(parent_observation, outcome="succeeded")
        assert child_result.ok is True

        job = background.start(
            lambda: make_text_result("shell", "background result"),
            session_id=parent.session.session_id,
            tool_name="shell",
            branch_context=parent_branch,
            dispatch_branch_context={
                "branch_id": parent_branch.branch_id,
                "branch_head_sequence_at_dispatch": parent.session.store.list_events(parent.session.session_id)[-1].sequence,
                "project_id": parent.session.rebuild_view().metadata["project_id"],
                "task_plan_revision": 0,
            },
            trace_recorder=recorder,
            trace_scope=parent_scope,
            parent_trace_id=parent_trace,
            parent_observation_id=parent_observation,
            session_writer=parent.session.writer,
            before_submit=lambda job: parent.session.writer.append_background_scheduled(
                job_id=job.id,
                tool_name=job.tool_name,
                dispatch_branch_context=job.dispatch_branch_context,
                parent_trace_id=parent_trace,
                parent_observation_id=parent_observation,
                branch_context=parent_branch,
            ),
        )
        assert background.wait(timeout=5) is True
        parent.runner.flush_background_notifications()
        assert job.status == "completed"

        query = ObservatoryQueryService(LansCoderPaths(storage_root=storage_root, project_root=project_a))
        sessions = query.list_sessions()
        session_ids = {item["session_id"] for item in sessions["items"]}
        assert session_ids == {"sess_primary_a", "sess_primary_b"}
        detail = query.get_trace(parent_trace)
        observation_types = {item["type"] for item in detail["observations"]}
        assert {"agent", "generation", "tool"} <= observation_types
        child_session_id = child_result.child_session_id
        child_trace_id = next(event.trace_id for event in parent.session.store.list_events(child_session_id) if event.kind == "trace.started")
        child_relations = [relation for observation in detail["observations"] for relation in observation["relations"]]
        assert any(relation["relation"] == "child" and relation["linked_trace_id"] == child_trace_id for relation in child_relations)
        child_detail = query.get_trace(child_trace_id)
        assert child_detail["parent_trace_id"] == parent_trace
        all_relations = detail["relations"] + [relation for observation in detail["observations"] for relation in observation["relations"]]
        assert any(relation["relation"] == "background" and relation["completion_status"] == "completed" for relation in all_relations), all_relations
        generation = next(item for item in detail["observations"] if item["type"] == "generation")
        assert generation["input"]["availability"] == "available"
        payload_url = generation["input"]["url"]
        digest = payload_url.split("/api/v1/payloads/", 1)[1].split("?", 1)[0]
        payload_size = int(payload_url.split("size_bytes=", 1)[1])
        raw_payload, _ = query.read_payload(digest, str(payload_size))
        assert json.loads(raw_payload)["messages"]
        raw_evidence = []
        for generation_observation in detail["observations"]:
            if generation_observation["type"] != "generation":
                continue
            raw_response = generation_observation["raw"]["ended_event"]["data"].get("provider_raw_response", {})
            raw_reference = raw_response.get("payload_ref")
            if raw_reference is None:
                continue
            provider_raw, _ = query.read_payload(raw_reference["sha256"], str(raw_reference["size_bytes"]))
            raw_evidence.append(json.loads(provider_raw))
        assert {"content": "permission resumed"} in raw_evidence
        replay = query.replay_session(parent.session.session_id)
        assert any(item["role"] == "notification" and item["status"] == "completed" for item in replay["items"])
        assert child_session_id not in session_ids
        with pytest.raises(SessionAccessError):
            ResumeService(parent.session.store, project_a).resume(child_session_id)
        with pytest.raises(SessionAccessError):
            ForkSessionService(parent.session.store, project_a).fork(child_session_id)
        project_a_traces = query.list_traces({"project": [project_id_for_path(project_a)]})
        project_b_traces = query.list_traces({"project": [project_id_for_path(project_b)]})
        assert {item["trace_id"] for item in project_a_traces["items"]} == {parent_trace, child_trace_id}
        other_trace = next(event.trace_id for event in other.session.store.list_events(other.session.session_id) if event.kind == "trace.started")
        assert {item["trace_id"] for item in project_b_traces["items"]} == {other_trace}
        with pytest.raises(SessionAccessError, match="another project"):
            ResumeService(parent.session.store, project_a).resume(other.session.session_id)
        with pytest.raises(SessionAccessError, match="another project"):
            ForkSessionService(parent.session.store, project_a).fork(other.session.session_id)
        assert other.session.session_id == "sess_primary_b"
    finally:
        background.shutdown()


class _UsageObject:
    def __init__(self, **values):
        self.__dict__.update(values)


def test_openai_anthropic_usage_details_and_l3_compaction_generation(tmp_path: Path) -> None:
    class OpenAICompletions:
        def create(self, **params):
            return _UsageObject(
                model=params["model"],
                usage=_UsageObject(
                    prompt_tokens=11,
                    completion_tokens=7,
                    total_tokens=18,
                    prompt_tokens_details=_UsageObject(cached_tokens=4, audio_tokens=1),
                    completion_tokens_details={"reasoning_tokens": 3},
                ),
                choices=[_UsageObject(finish_reason="stop", message=_UsageObject(content="done", tool_calls=[]))],
            )

    class AnthropicMessages:
        def create(self, **params):
            return _UsageObject(
                model=params["model"],
                stop_reason="end_turn",
                usage=_UsageObject(input_tokens=20, output_tokens=8, cache_creation_input_tokens=12, cache_read_input_tokens=9),
                content=[_UsageObject(type="text", text="done")],
            )

    openai = OpenAICompatibleProvider(
        name="fake-openai",
        model="fake-model",
        api_key="fake",
        client=_UsageObject(chat=_UsageObject(completions=OpenAICompletions())),
    )
    anthropic = AnthropicProvider(
        model="fake-claude",
        api_key="fake",
        client=_UsageObject(messages=AnthropicMessages()),
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    assert openai.complete(request).usage.usage_details == {
        "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 1},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    assert anthropic.complete(request).usage.usage_details == {
        "cache_creation_input_tokens": 12,
        "cache_read_input_tokens": 9,
    }

    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_l3_integration")
    writer.append_session_created(project_id=paths.project_id, kind="primary")
    recorder = JournalTraceRecorder(store.journal, PayloadStore(paths))
    scope = TraceScope("sess_l3_integration", writer.branch_context.branch_id)
    trace_id = recorder.start_trace(scope, data={"operation": "agent_turn"})
    messages = [
        AgentMessage(
            id="message_old",
            session_id="sess_l3_integration",
            role="user",
            parts=[MessagePart(id="part_old", message_id="message_old", kind="text", content="old", metadata={"created_turn": 1})],
        ),
        AgentMessage(
            id="message_recent",
            session_id="sess_l3_integration",
            role="user",
            parts=[MessagePart(id="part_recent", message_id="message_recent", kind="text", content="recent", metadata={"created_turn": 20})],
        ),
    ]
    summary = "## 用户请求要点\n- old\n\n## 已给出的结论\n- done\n\n## 未完成事项\n- 无\n\n## 关键约束与偏好\n- 无"
    summarizer = ProviderLlmCompactSummarizer(
        FakeProvider([_response(summary, usage=TokenUsage(input_tokens=7, output_tokens=3, total_tokens=10, usage_details={"completion_tokens_details": {"reasoning_tokens": 2}}))]),
        trace_recorder=recorder,
    )
    candidate = LlmCompactService(store=store, summarizer=summarizer).generate_candidate(
        LlmCompactRequest(
            view=SessionView(session_id="sess_l3_integration", messages=messages),
            runtime_state=SessionRuntimeState(session_id="sess_l3_integration"),
            consumed_tool_result_part_ids=frozenset(),
            current_turn=20,
            recent_turn_window=1,
            trace_scope=scope,
            trace_id=trace_id,
        )
    )
    assert candidate.event.status == "success"
    assert candidate.checkpoint is not None
    record = project_trace(store.list_events("sess_l3_integration"), trace_id, payload_store=PayloadStore(paths))
    generation = next(item for item in record.observations if item.data.get("operation") == "compaction")
    assert generation.data["usage_details"] == {"completion_tokens_details": {"reasoning_tokens": 2}}


def test_cli_access_policy_index_rebuild_and_query_equivalence(tmp_path: Path) -> None:
    storage_root = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    paths = LansCoderPaths(storage_root=storage_root, project_root=project)
    store = JsonlSessionStore(storage_root)
    primary = SessionEventWriter(store=store, session_id="sess_cli_primary")
    primary.append_session_created(project_id=paths.project_id, kind="primary", title="CLI")
    recorder = JournalTraceRecorder(store.journal, PayloadStore(paths))
    trace_id = recorder.start_trace(TraceScope("sess_cli_primary", primary.branch_context.branch_id), data={"project_id": paths.project_id, "input": "hello"})
    observation_id = recorder.start_observation(trace_id, "generation", data={"provider": "fake", "model": "fake-model", "normalized_request": {"messages": [{"role": "user", "content": "hello"}]}})
    recorder.end_observation(observation_id, outcome="succeeded", data={"normalized_response": {"content": "done"}, "usage": {"total_tokens": 1}, "usage_details": {}})
    recorder.end_trace(trace_id, final_output="done")
    child = SessionEventWriter(store=store, session_id="sess_cli_child")
    child.append_session_created(project_id=paths.project_id, kind="subagent", parent_session_id="sess_cli_primary")

    catalog = SessionCatalog(storage_root)
    policy = SessionAccessPolicy(project, journal=catalog)
    assert policy.open_primary("sess_cli_primary").kind == "primary"
    with pytest.raises(SessionAccessError, match="not a primary"):
        policy.open_primary("sess_cli_child")
    with pytest.raises(SessionAccessError, match="not a primary"):
        ResumeService(store=store, project_root=project).resume("sess_cli_child")
    with pytest.raises(SessionAccessError, match="not a primary"):
        ForkSessionService(store=store, project_root=project).fork("sess_cli_child")

    original_factory = cli.create_lanscoder_app

    def fake_factory(**kwargs):
        return original_factory(provider=FakeProvider([_response("unused")]), tools=[], **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cli, "create_lanscoder_app", fake_factory)
    try:
        assert (
            cli.main(
                [
                    "--project",
                    str(project),
                    "--storage-root",
                    str(storage_root),
                    "--session-id",
                    "sess_cli_child",
                    "--resume-session",
                    "--message",
                    "resume",
                ]
            )
            == 1
        )
    finally:
        monkeypatch.undo()

    app = create_lanscoder_app(
        project_root=project,
        storage_root=storage_root,
        provider=FakeProvider([_response("unused")]),
        session_id="sess_cli_primary",
        resume_session=True,
        tools=[],
    )
    try:
        resume_result = app.command_handler.handle("/resume sess_cli_child")
        assert "not a primary session: sess_cli_child" in resume_result.output
        session_handler = next(handler for handler in app.command_handler.handlers if handler.__class__.__name__ == "SessionCommandHandler")
        session_handler.current_session = type("CurrentSession", (), {"session_id": "sess_cli_child"})()
        fork_result = app.command_handler.handle("/fork")
        assert "not a primary session: sess_cli_child" in fork_result.output
    finally:
        app.on_unmount()

    query = ObservatoryQueryService(paths)
    indexed = {
        "sessions": query.list_sessions(),
        "traces": query.list_traces({}),
        "trace": query.get_trace(trace_id),
        "replay": query.replay_session("sess_cli_primary"),
    }
    shutil.rmtree(paths.indexes)
    recreated = ObservatoryQueryService(paths)
    rebuilt = {
        "sessions": recreated.list_sessions(),
        "traces": recreated.list_traces({}),
        "trace": recreated.get_trace(trace_id),
        "replay": recreated.replay_session("sess_cli_primary"),
    }
    assert rebuilt == indexed


def test_incomplete_tail_retains_evidence_and_marks_open_trace_incomplete(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JournalStore(paths, "sess_tail_integration")
    store.append("session.created", {"root_branch_id": "brn_root", "kind": "primary"}, branch_id="brn_root")
    store.append("trace.started", {"status": "running"}, trace_id="trc_tail", branch_id="brn_root")
    store.append(
        "observation.started",
        {"observation_type": "generation", "normalized_request": {"messages": []}},
        trace_id="trc_tail",
        observation_id="obs_tail",
        branch_id="brn_root",
    )
    tail = b'{"schema_version":1,"sequence":4'
    with paths.session("sess_tail_integration").open("ab") as handle:
        handle.write(tail)

    events = store.read_events()
    recovery = next(event for event in events if event.kind == "journal.recovered")
    assert recovery.data["tail_sha256"] == hashlib.sha256(tail).hexdigest()
    assert recovery.data["start_byte"] < recovery.data["end_byte"]
    evidence_path = Path(recovery.data["evidence_path"])
    assert evidence_path.parent == paths.recovery_tails
    assert evidence_path.read_bytes() == tail
    record = project_trace(events, "trc_tail")
    assert record.incomplete is True
    assert any(observation.observation_id == "obs_tail" and observation.ended_at is None for observation in record.observations)


def test_middle_journal_corruption_is_refused_and_diagnosable(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JournalStore(paths, "sess_middle_corrupt")
    store.append("session.created", {"root_branch_id": "brn_root", "kind": "primary"}, branch_id="brn_root")
    store.append("trace.started", {"status": "running"}, trace_id="trc_corrupt", branch_id="brn_root")
    store.append("trace.ended", {"status": "completed", "outcome": "succeeded", "final_output": "done"}, trace_id="trc_corrupt", branch_id="brn_root")
    path = paths.session("sess_middle_corrupt")
    lines = path.read_bytes().splitlines(keepends=True)
    lines[1] = b"{corrupted-middle-line}\n"
    path.write_bytes(b"".join(lines))

    with pytest.raises(JournalCorruptError, match="line 2"):
        store.read_events()
    assert path.read_bytes().splitlines(keepends=True)[1] == b"{corrupted-middle-line}\n"
    record = SessionCatalog(paths.storage_root).get_session("sess_middle_corrupt")
    assert record.status == "corrupt"
    diagnostic = ObservatoryQueryService(paths).list_sessions()
    assert any(item["session_id"] == "sess_middle_corrupt" for item in diagnostic["diagnostics"])
    with pytest.raises(ObservatoryProblem) as error:
        ObservatoryQueryService(paths).replay_session("sess_middle_corrupt")
    assert error.value.code == "journal_corrupt"
