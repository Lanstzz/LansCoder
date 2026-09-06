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


def test_turn_observer_records_denied_tool_as_event_observation() -> None:
    from lanscoder.agent.observer import TurnObserver

    recorder = RecordingRecorder()
    observer = TurnObserver(trace_recorder=recorder, trace_id="trace_1")
    call = ToolCall(id="call_1", name="shell", arguments={"command": "false"})
    result = make_text_result("shell", "denied", ok=False, error="permission denied")

    observer.on_tool_event(ToolExecutionEvent(kind="denied", tool_call=call, result=result))

    assert [kind for kind, _, _ in recorder.events] == [
        "observation.started:event",
        "observation.ended",
    ]
    assert recorder.events[-1][2]["outcome"] == "failed"
