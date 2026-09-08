from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lanscoder.agent.session import AgentSession
from lanscoder.agent.tool_execution import ToolExecutionEvent
from lanscoder.core.runtime import AgentChatRunner, CurrentSessionState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, TokenUsage, ToolCall
from lanscoder.tools.types import make_text_result
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.core.session import create_agent_session


@dataclass
class FakeProvider(ChatProvider):
    response: ChatResponse

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        return self.response


@dataclass
class RecordingRecorder:
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    observation_count: int = 0

    def start_trace(self, scope, *, trace_id=None, data=None) -> str:
        self.events.append(("trace.started", trace_id or "trace_1", dict(data or {})))
        return trace_id or "trace_1"

    def pause_trace(self, trace_id, *, pending=None) -> None:
        self.events.append(("trace.paused", trace_id, dict(pending or {})))

    def resume_trace(self, trace_id) -> None:
        self.events.append(("trace.resumed", trace_id, {}))

    def end_trace(self, trace_id, **kwargs) -> None:
        self.events.append(("trace.ended", trace_id, dict(kwargs)))

    def start_observation(self, trace_id, observation_type, *, data=None, **kwargs) -> str:
        self.observation_count += 1
        observation_id = f"observation_{self.observation_count}"
        self.events.append((f"observation.started:{observation_type}", observation_id, dict(data or {})))
        return observation_id

    def end_observation(self, observation_id, *, outcome, data=None, error=None) -> None:
        self.events.append(("observation.ended", observation_id, {"outcome": outcome, **(data or {})}))

    def link_trace(self, parent_trace_id, child_trace_id, *, relation="child", data=None) -> None:
        self.events.append(("trace.linked", child_trace_id, dict(data or {})))


@dataclass
class ReturnedTraceIdRecorder(RecordingRecorder):
    returned_trace_id: str = "recorder_trace"
    observed_trace_ids: list[tuple[str, str]] = field(default_factory=list)

    def start_trace(self, scope, *, trace_id=None, data=None) -> str:
        self.events.append(("trace.started", self.returned_trace_id, dict(data or {})))
        return self.returned_trace_id

    def start_observation(self, trace_id, observation_type, *, data=None, **kwargs) -> str:
        self.observed_trace_ids.append((trace_id, str(observation_type)))
        return super().start_observation(trace_id, observation_type, data=data, **kwargs)

    def resume_trace(self, trace_id, *, scope=None) -> None:
        self.events.append(("trace.resumed", trace_id, {}))


class FailingRecorder(RecordingRecorder):
    def start_trace(self, scope, *, trace_id=None, data=None) -> str:
        raise OSError("observability unavailable")


@dataclass
class ErrorProvider(ChatProvider):
    error: BaseException

    @property
    def name(self) -> str:
        return "error-provider"

    @property
    def model(self) -> str:
        return "error-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise self.error


def test_runner_records_root_and_generation_observations(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    session = AgentSession.create(store=store, session_id="sess_runtime")
    recorder = RecordingRecorder()
    provider = FakeProvider(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="done",
            usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
        )
    )
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=provider,
        trace_recorder=recorder,
    )

    response = runner.run_user_turn("hello")

    assert response.content == "done"
    kinds = [kind for kind, _, _ in recorder.events]
    assert kinds[0] == "trace.started"
    assert "observation.started:generation" in kinds
    assert "observation.ended" in kinds
    assert kinds[-1] == "trace.ended"
    generation_data = next(data for kind, _, data in recorder.events if kind == "observation.started:generation")
    assert generation_data["normalized_request"]["messages"]
    trace_end = recorder.events[-1][2]
    assert trace_end["final_output"]["content"] == "done"


def test_runner_uses_recorder_returned_trace_id_for_agent_pause_resume_and_end(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    provider = FakeProvider(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[ToolCall(id="call_ask", name="ask_user", arguments={"question": "Continue?"})],
            finish_reason="tool_calls",
        )
    )
    session = AgentSession.create(store=store, session_id="sess_returned_trace_id", tools=[create_ask_user_tool()])
    recorder = ReturnedTraceIdRecorder()
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=provider,
        trace_recorder=recorder,
    )

    assert runner.run_user_turn("hello").finish_reason == "waiting_for_user_input"
    provider.response = ChatResponse(provider="fake", model="fake-model", content="done")
    assert runner.resume_with_user_input("call_ask", "answer").content == "done"

    assert recorder.observed_trace_ids
    assert {trace_id for trace_id, _ in recorder.observed_trace_ids} == {"recorder_trace"}
    assert [event[1] for event in recorder.events if event[0] == "trace.paused"] == ["recorder_trace"]
    assert [event[1] for event in recorder.events if event[0] == "trace.resumed"] == ["recorder_trace"]
    assert [event[1] for event in recorder.events if event[0] == "trace.ended"] == ["recorder_trace"]


def test_turn_observer_records_denied_tool_and_child_event_observations() -> None:
    from lanscoder.agent.observer import TurnObserver

    recorder = RecordingRecorder()
    observer = TurnObserver(trace_recorder=recorder, trace_id="trace_1")
    call = ToolCall(id="call_1", name="shell", arguments={"command": "false"})
    result = make_text_result("shell", "denied", ok=False, error="permission denied")

    observer.on_tool_event(ToolExecutionEvent(kind="denied", tool_call=call, result=result))

    assert [kind for kind, _, _ in recorder.events] == [
        "observation.started:tool",
        "observation.started:event",
        "observation.ended",
        "observation.ended",
    ]
    tool_start = recorder.events[0]
    event_start = recorder.events[1]
    assert tool_start[2]["tool_call_id"] == "call_1"
    assert event_start[2]["tool_call_id"] == "call_1"
    assert recorder.events[-2][2]["outcome"] == "failed"
    assert recorder.events[-1][2]["outcome"] == "failed"


def test_default_runtime_assembles_journal_recorder_and_agent_observation(tmp_path) -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="done"))
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
    )

    response = handle.runner.run_user_turn("hello")

    assert response.content == "done"
    events = handle.session.store.journal.read_events(handle.session.session_id)
    kinds = [event.kind for event in events]
    assert "trace.started" in kinds
    assert "observation.started" in kinds
    assert "trace.ended" in kinds
    agent_events = [event for event in events if event.kind == "observation.started" and event.data.get("observation_type") == "agent"]
    assert len(agent_events) == 1


def test_ask_user_pause_persists_identity_and_resumes_same_trace_after_restart(tmp_path) -> None:
    provider = FakeProvider(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[ToolCall(id="call_ask", name="ask_user", arguments={"question": "继续吗？"})],
            finish_reason="tool_calls",
        )
    )
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[create_ask_user_tool()],
    )

    waiting = handle.runner.run_user_turn("请询问")
    assert waiting.finish_reason == "waiting_for_user_input"
    events = handle.session.store.journal.read_events(handle.session.session_id)
    paused = next(event for event in events if event.kind == "trace.paused")
    trace_id = paused.trace_id
    assert paused.data["pending"]["trace_id"] == trace_id
    assert paused.data["pending"]["tool_call_id"] == "call_ask"
    assert paused.data["pending"]["pending_kind"] == "ask_user"

    resumed_handle = create_agent_session(
        provider=FakeProvider(ChatResponse(provider="fake", model="fake-model", content="finished")),
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        session_id=handle.session.session_id,
        resume=True,
        tools=[create_ask_user_tool()],
    )
    response = resumed_handle.runner.resume_with_user_input("call_ask", "继续")

    assert response.content == "finished"
    resumed_events = resumed_handle.session.store.journal.read_events(handle.session.session_id)
    assert any(event.kind == "trace.resumed" and event.trace_id == trace_id for event in resumed_events)
    assert any(event.kind == "trace.ended" and event.trace_id == trace_id for event in resumed_events)


def test_failing_recorder_does_not_change_agent_result(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    session = AgentSession.create(store=store, session_id="sess_fail_open")
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=FakeProvider(ChatResponse(provider="fake", model="fake-model", content="done")),
        trace_recorder=FailingRecorder(),
    )

    response = runner.run_user_turn("hello")

    assert response.content == "done"
    assert [message.parts[0].content for message in session.rebuild_view().messages] == ["hello", "done"]


def test_interrupted_root_trace_has_cancelled_status_and_reason(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    session = AgentSession.create(store=store, session_id="sess_cancelled_trace")
    recorder = RecordingRecorder()
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=FakeProvider(ChatResponse(provider="fake", model="fake-model", content="stopped", finish_reason="interrupted")),
        trace_recorder=recorder,
    )

    runner.run_user_turn("stop")

    end = next(data for kind, _, data in recorder.events if kind == "trace.ended")
    assert end["status"] == "cancelled"
    assert end["reason"]["code"] == "interrupted"


def test_provider_exception_ends_root_trace_as_failed(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "storage")
    session = AgentSession.create(store=store, session_id="sess_failed_trace")
    recorder = RecordingRecorder()
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=ErrorProvider(RuntimeError("provider failed")),
        trace_recorder=recorder,
    )

    try:
        runner.run_user_turn("fail")
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider error must reach the caller")

    end = next(data for kind, _, data in recorder.events if kind == "trace.ended")
    assert end["status"] == "failed"
    assert str(end["error"]) == "provider failed"
