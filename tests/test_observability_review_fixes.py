"""Focused regressions for the Stage 4b review fixes."""

from __future__ import annotations

from dataclasses import dataclass, field

from lanscoder.agent.observer import TurnObserver
from lanscoder.agent.tool_execution import ToolExecutionEvent
from lanscoder.context.compaction import CompactionEvent, CompactionResult
from lanscoder.context.llm_compact import LlmCompactRequest, LlmCompactService
from lanscoder.context.manager import ContextCompactMode, ContextCompactRequest, ContextWindowManager, ContextWindowTrigger
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.token_budget import ContextBudget
from lanscoder.context.writer import SessionEventWriter
from lanscoder.core.session import create_agent_session
from lanscoder.observability import JournalTraceRecorder, TraceScope, project_trace
from lanscoder.observability.models import ObservationType
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatRequest, ChatResponse, TokenUsage, ToolCall
from lanscoder.storage import LansCoderPaths, PayloadStore
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.tools.ls import create_ls_tool
from lanscoder.tools.view import create_view_tool
from lanscoder.tools.types import make_error_result
from lanscoder.tools.write import create_write_tool


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse | BaseException]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(content: str = "done") -> ChatResponse:
    return ChatResponse(
        provider="fake",
        model="fake-model",
        content=content,
        finish_reason="stop",
        usage=TokenUsage(
            input_tokens=3,
            output_tokens=2,
            total_tokens=5,
            usage_details={"completion_tokens_details": {"reasoning_tokens": 1}},
        ),
    )


def _tool_response(tool_call: ToolCall) -> ChatResponse:
    return ChatResponse(
        provider="fake",
        model="fake-model",
        content="",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )


def _trace_events(handle):
    return handle.session.store.journal.read_events(handle.session.session_id)


def _trace_record(handle):
    events = _trace_events(handle)
    trace_id = next(event.trace_id for event in events if event.kind == "trace.started")
    return events, project_trace(
        events,
        trace_id,
        payload_store=PayloadStore(LansCoderPaths(storage_root=handle.session.store.root)),
    )


def test_prewrite_review_events_cover_wait_resume_and_failed_preview(tmp_path):
    write_call = ToolCall(id="call-write", name="write", arguments={"path": "result.txt", "content": "new"})
    provider = FakeProvider([_tool_response(write_call), _response()])
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[create_write_tool(tmp_path)],
        compaction_strategy="no_compact",
    )

    waiting = handle.runner.run_user_turn("write")
    assert waiting.finish_reason == "waiting_for_user_input"
    request_id = handle.runner.last_pending_input.id
    assert handle.runner.resume_with_user_input(request_id, "allow_once").content == "done"

    events = _trace_events(handle)
    preview_events = [
        event
        for event in events
        if event.kind == "observation.started"
        and event.data.get("observation_type") == "event"
        and event.data.get("event") == "prewrite_review"
    ]
    assert len(preview_events) == 2
    assert all(event.data["arguments"] == write_call.arguments for event in preview_events)
    assert all(event.data["prewrite_review"]["error"] is None for event in preview_events)
    preview_ends = {
        event.observation_id: event.data["outcome"]
        for event in events
        if event.kind == "observation.ended"
        and event.observation_id in {item.observation_id for item in preview_events}
    }
    assert set(preview_ends.values()) == {"succeeded"}

    bad_call = ToolCall(id="call-bad-write", name="write", arguments={"path": ".", "content": "new"})
    bad_handle = create_agent_session(
        provider=FakeProvider([_tool_response(bad_call), _response()]),
        project_root=tmp_path,
        storage_root=tmp_path / "bad-storage",
        tools=[create_write_tool(tmp_path)],
        compaction_strategy="no_compact",
    )
    assert bad_handle.runner.run_user_turn("write directory").content == "done"
    bad_events, bad_record = _trace_record(bad_handle)
    failed_preview = next(
        event
        for event in bad_events
        if event.kind == "observation.started"
        and event.data.get("observation_type") == "event"
        and event.data.get("event") == "prewrite_review"
    )
    assert failed_preview.data["arguments"] == bad_call.arguments
    assert failed_preview.data["prewrite_review"]["error"]
    assert next(item for item in bad_record.observations if item.observation_id == failed_preview.observation_id).outcome == "failed"


def test_ask_user_answer_is_child_of_final_tool_observation(tmp_path):
    ask_call = ToolCall(id="call-ask", name="ask_user", arguments={"question": "Continue?"})
    handle = create_agent_session(
        provider=FakeProvider([_tool_response(ask_call), _response()]),
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[create_ask_user_tool()],
        compaction_strategy="no_compact",
    )

    assert handle.runner.run_user_turn("ask").finish_reason == "waiting_for_user_input"
    assert handle.runner.resume_with_user_input("call-ask", "yes").content == "done"

    events, record = _trace_record(handle)
    tools = [item for item in record.observations if item.observation_type is ObservationType.TOOL and item.data.get("tool_call_id") == "call-ask"]
    assert len(tools) == 2
    final_tool = tools[-1]
    assert final_tool.outcome == "succeeded"
    answer = next(item for item in record.observations if item.data.get("event") == "ask_user_answer")
    assert answer.parent_observation_id == final_tool.observation_id
    assert any(
        event.kind == "observation.started"
        and event.data.get("observation_type") == "tool"
        and event.observation_id == final_tool.observation_id
        and event.data["arguments"] == ask_call.arguments
        for event in events
    )


def test_parallel_readonly_tools_have_independent_completed_observations(tmp_path):
    (tmp_path / "read.txt").write_text("hello\n", encoding="utf-8")
    calls = [
        ToolCall(id="call-ls", name="ls", arguments={"path": "."}),
        ToolCall(id="call-view", name="view", arguments={"path": "read.txt"}),
    ]
    handle = create_agent_session(
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="", finish_reason="tool_calls", tool_calls=calls), _response()]),
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[create_ls_tool(tmp_path), create_view_tool(tmp_path)],
        compaction_strategy="no_compact",
    )

    assert handle.runner.run_user_turn("inspect").content == "done"
    events, record = _trace_record(handle)
    tools = {
        item.data["tool_call_id"]: item
        for item in record.observations
        if item.observation_type is ObservationType.TOOL
    }
    assert set(tools) == {"call-ls", "call-view"}
    for call in calls:
        observation = tools[call.id]
        assert observation.outcome == "succeeded"
        assert observation.data["arguments"] == call.arguments
        child = next(item for item in record.observations if item.data.get("event") == "finished" and item.data.get("tool_call_id") == call.id)
        assert child.parent_observation_id == observation.observation_id
        assert any(event.kind == "observation.ended" and event.observation_id == observation.observation_id for event in events)


def test_rejected_background_call_records_denied_lifecycle_and_original_arguments(tmp_path):
    call = ToolCall(
        id="call-background",
        name="missing_tool",
        arguments={"value": "x", "run_in_background": True, "background_label": "test"},
    )
    handle = create_agent_session(
        provider=FakeProvider([_tool_response(call), _response()]),
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[],
        compaction_strategy="no_compact",
    )

    assert handle.runner.run_user_turn("background").content == "done"
    events, record = _trace_record(handle)
    tool = next(item for item in record.observations if item.observation_type is ObservationType.TOOL)
    assert tool.outcome == "failed"
    assert tool.data["arguments"] == call.arguments
    assert any(item.data.get("event") == "denied" and item.parent_observation_id == tool.observation_id for item in record.observations)


def test_interrupted_synthetic_tool_observation_keeps_arguments():
    class Recorder:
        def __init__(self):
            self.started = []

        def start_observation(self, trace_id, observation_type, *, data=None, **kwargs):
            self.started.append((observation_type, dict(data or {})))
            return "tool-observation"

        def end_observation(self, observation_id, *, outcome, data=None, error=None):
            return None

    recorder = Recorder()
    observer = TurnObserver(trace_recorder=recorder, trace_id="trace")
    call = ToolCall(id="call-interrupted", name="shell", arguments={"command": "sleep 1"})
    observer.on_tool_event(
        ToolExecutionEvent(
            kind="interrupted",
            tool_call=call,
            result=make_error_result("shell", "interrupted", interrupted=True),
        )
    )
    assert recorder.started[0][1]["arguments"] == call.arguments


def test_failed_manual_compaction_trace_has_structured_reason(tmp_path):
    store = JsonlSessionStore(tmp_path / "storage")
    writer = SessionEventWriter(store=store, session_id="sess-compaction")
    writer.append_session_created()
    view = SessionView(
        session_id="sess-compaction",
        messages=[
            AgentMessage(
                id="message-1",
                session_id="sess-compaction",
                role="user",
                parts=[MessagePart(id="part-1", message_id="message-1", kind="text", content="history")],
            )
        ],
    )
    recorder = JournalTraceRecorder(store.journal, PayloadStore(LansCoderPaths(storage_root=store.root)))
    manager = ContextWindowManager(
        store=store,
        pipeline=_OverBudgetPipeline(view),
        l3_service=None,
        trace_recorder=recorder,
    )
    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess-compaction"),
            budget=_budget(100),
            estimate_budget=lambda candidate: _budget(100),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
            target_tokens=60,
        )
    )
    assert result.status == "failed"
    events = store.journal.read_events("sess-compaction")
    ended = next(event for event in events if event.kind == "trace.ended")
    assert ended.data["reason"]["code"]
    record = project_trace(events, ended.trace_id, payload_store=PayloadStore(LansCoderPaths(storage_root=store.root)))
    assert not record.incomplete


def test_l3_retry_generation_ends_include_diagnostics_and_usage_details(tmp_path):
    summary = "## 用户请求要点\n- hello\n\n## 已给出的结论\n- done\n\n## 未完成事项\n- 无\n\n## 关键约束与偏好\n- 无"
    provider = FakeProvider([ProviderError(ProviderErrorKind.TIMEOUT, "timeout"), _response(summary)])
    store = JsonlSessionStore(tmp_path / "storage")
    writer = SessionEventWriter(store=store, session_id="sess-l3")
    writer.append_session_created()
    messages = [
        AgentMessage(
            id="message-1",
            session_id="sess-l3",
            role="user",
            parts=[MessagePart(id="part-1", message_id="message-1", kind="text", content="hello", metadata={"created_turn": 1})],
        ),
        AgentMessage(
            id="message-2",
            session_id="sess-l3",
            role="user",
            parts=[MessagePart(id="part-2", message_id="message-2", kind="text", content="world", metadata={"created_turn": 20})],
        ),
    ]
    recorder = JournalTraceRecorder(store.journal, PayloadStore(LansCoderPaths(storage_root=store.root)))
    scope = TraceScope("sess-l3", writer.branch_context.branch_id)
    trace_id = recorder.start_trace(scope)
    service = LlmCompactService(
        store=store,
        summarizer=ProviderLlmCompactSummarizer(provider, trace_recorder=recorder),
    )
    service.generate_candidate(
        LlmCompactRequest(
            view=SessionView(session_id="sess-l3", messages=messages),
            runtime_state=SessionRuntimeState(session_id="sess-l3"),
            consumed_tool_result_part_ids=frozenset(),
            current_turn=20,
            recent_turn_window=1,
            trace_scope=scope,
            trace_id=trace_id,
        )
    )
    events = store.journal.read_events("sess-l3")
    records = project_trace(events, trace_id, payload_store=PayloadStore(LansCoderPaths(storage_root=store.root)))
    generations = [item for item in records.observations if item.observation_type is ObservationType.GENERATION]
    assert len(generations) == 2
    assert generations[0].data["diagnostics"] == {}
    assert generations[0].data["usage_details"] == {}
    assert generations[1].data["diagnostics"]
    assert generations[1].data["usage_details"] == {"completion_tokens_details": {"reasoning_tokens": 1}}


class _OverBudgetPipeline:
    def __init__(self, view: SessionView) -> None:
        self.view = view

    def compact(self, request):
        return CompactionResult(
            view=self.view,
            event=CompactionEvent(
                input_fingerprint="input",
                before_tokens=100,
                after_tokens=100,
                levels_attempted=["l1"],
                stopped_at="l1",
                changed_parts=0,
            ),
        )


def _budget(tokens: int) -> ContextBudget:
    return ContextBudget(
        context_window=32_768,
        output_reserve=4_096,
        input_capacity=27_033,
        fixed_tokens=10,
        history_tokens=max(0, tokens - 10),
        input_tokens=tokens,
        high_watermark=100,
        low_watermark=60,
        source="configured",
    )
