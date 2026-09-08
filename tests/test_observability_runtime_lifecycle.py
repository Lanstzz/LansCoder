"""Stage 4a acceptance tests against durable journals and fake providers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import pytest

from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.context.writer import SessionEventWriter
from lanscoder.core.session import create_agent_session
from lanscoder.observability.context import get_observation_id, get_trace_id, get_trace_scope
from lanscoder.observability.models import TraceRecord
from lanscoder.observability.protocol import NoOpTraceRecorder
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, ToolCall
from lanscoder.storage import LansCoderPaths, PayloadStore
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.tools.types import ToolResult, make_text_result
from lanscoder.tools.write import create_write_tool
from lanscoder.utils.cancellation import AgentCancelledError
from lanscoder.utils.introspection import tool_from_function


@dataclass
class ScriptedProvider(ChatProvider):
    responses: list[ChatResponse | BaseException]
    requests: list[ChatRequest] = field(default_factory=list)
    contexts: list[tuple] = field(default_factory=list)

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    def complete(self, request):
        self.requests.append(request)
        self.contexts.append((get_trace_scope(), get_trace_id(), get_observation_id()))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def astream(self, request):
        response = self.complete(request)
        yield ChatStreamEvent(kind="message_completed", response=response)


def reply(content="done", *, finish_reason="stop", calls=()):
    return ChatResponse(provider="fake", model="fake-model", content=content, finish_reason=finish_reason, tool_calls=list(calls))


def question(call_id="ask"):
    return reply("", finish_reason="tool_calls", calls=[ToolCall(id=call_id, name="ask_user", arguments={"question": "Continue?"})])


def write_request():
    return reply("", finish_reason="tool_calls", calls=[ToolCall(id="write", name="write", arguments={"path": "result.txt", "content": "new"})])


@pytest.fixture
def runtime(tmp_path):
    def make(responses=(), **kwargs):
        resolved_tools = kwargs.pop("tools", [create_ask_user_tool(), create_write_tool(tmp_path)])
        return create_agent_session(
            provider=ScriptedProvider(list(responses)),
            project_root=tmp_path,
            storage_root=tmp_path / "storage",
            tools=resolved_tools,
            compaction_strategy="no_compact",
            **kwargs,
        )

    return make


def traces(handle):
    events = handle.session.store.journal.read_events(handle.session.session_id)
    payloads = PayloadStore(LansCoderPaths(storage_root=handle.session.store.root))
    records = [TraceRecord.from_events(events, event.trace_id, payload_store=payloads) for event in events if event.kind == "trace.started"]
    return events, records


@pytest.mark.parametrize("streaming", [False, True])
def test_root_records_input_and_captured_branch_and_closes_agent(runtime, streaming):
    handle = runtime([reply()])
    handle.runner.use_streaming = streaming
    response = handle.runner.run_user_turn("original input")

    events, records = traces(handle)
    assert response.content == "done"
    assert len(records) == 1
    assert records[0].status == "completed"
    assert not records[0].incomplete
    start = next(event for event in events if event.kind == "trace.started")
    assert start.data["input"] == "original input"
    assert start.branch_id == handle.session.writer.branch_context.branch_id
    agent = next(obs for obs in records[0].observations if obs.observation_type == "agent")
    assert agent.outcome == "succeeded"
    assert agent.duration_ms is not None
    assert handle.runner.provider.contexts[0][1:] == (start.trace_id, agent.observation_id)
    assert get_trace_id() is None


@pytest.mark.parametrize("error,status", [(RuntimeError("failed"), "failed"), (asyncio.CancelledError("cancelled"), "cancelled")])
def test_exception_preserves_cause_and_closes_root_and_agent(runtime, error, status):
    handle = runtime([error])
    with pytest.raises(type(error)) as caught:
        handle.runner.run_user_turn("input")
    assert caught.value is error
    _, records = traces(handle)
    assert records[0].status == status
    assert records[0].ended_at is not None
    assert records[0].error["message"] == str(error)
    assert next(obs for obs in records[0].observations if obs.observation_type == "agent").outcome == status
    assert handle.runner._active_cancellation_token is None


def test_cooperative_cancellation_closes_trace_with_reason(runtime):
    handle = runtime([AgentCancelledError()])
    response = handle.runner.run_user_turn("cancel")
    _, records = traces(handle)
    assert response.finish_reason == "interrupted"
    assert records[0].status == "cancelled"
    assert records[0].reason
    assert records[0].ended_at is not None
    assert next(obs for obs in records[0].observations if obs.observation_type == "agent").outcome == "cancelled"


def test_zero_provider_limit_records_no_generation_reason(runtime):
    handle = runtime(limits=AgentLoopLimits(max_provider_calls=0))
    response = handle.runner.run_user_turn("input")
    _, records = traces(handle)
    assert response.finish_reason == "provider_call_limit"
    assert handle.runner.provider.requests == []
    assert records[0].outcome == "no_generation"
    assert records[0].reason["code"] == "provider_call_limit"
    assert not records[0].incomplete


def test_empty_nudge_closes_no_generation_trace(runtime):
    handle = runtime()
    assert asyncio.run(handle.runner.anudge_turn()).content == ""
    _, records = traces(handle)
    assert len(records) == 1
    assert records[0].outcome == "no_generation"
    assert not records[0].incomplete
    assert handle.runner._active_trace_id is None


@pytest.mark.parametrize("failure", ["setup", "finish"])
def test_session_errors_during_setup_or_finish_close_trace(runtime, monkeypatch, failure):
    handle = runtime([reply()])
    if failure == "setup":

        def fail_tools():
            raise RuntimeError("setup failed")

        handle.runner.tools_provider = fail_tools
    else:
        original = handle.session.rebuild_view

        def fail_after_response():
            view = original()
            if any(message.role == "assistant" for message in view.messages):
                raise RuntimeError("finish failed")
            return view

        monkeypatch.setattr(type(handle.session), "rebuild_view", lambda self: fail_after_response())
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        handle.runner.run_user_turn("input")
    _, records = traces(handle)
    assert records[0].status == "failed"
    assert not records[0].incomplete
    assert handle.runner._active_cancellation_token is None


@pytest.mark.parametrize("streaming", [False, True])
def test_permission_restart_preserves_request_identity_and_decision(runtime, tmp_path, monkeypatch, streaming):
    handle = runtime([write_request()])
    handle.runner.use_streaming = streaming
    manager = handle.session.permission_coordinator.permission_manager
    original = manager.normalize_request
    monkeypatch.setattr(manager, "normalize_request", lambda request: replace(original(request), id="persisted-request"))
    handle.runner.run_user_turn("write")
    pending_id = handle.runner.last_pending_input.id
    events, paused_records = traces(handle)
    assert not paused_records[0].incomplete
    pause = next(event for event in events if event.kind == "trace.paused")
    tool_part = next(part for message in handle.session.rebuild_view().messages for part in message.parts if part.kind == "tool_call")
    assert tool_part.metadata["trace_id"] == pause.trace_id
    assert tool_part.metadata["pending_kind"] == "permission_confirmation"
    assert tool_part.metadata["request_id"] == pending_id

    resumed = runtime([reply()], session_id=handle.session.session_id, resume=True)
    assert resumed.session.pending_permission_execution.request_id == pending_id
    assert resumed.runner.sync_pending_input_from_current_session().id == pending_id
    response = resumed.runner.resume_with_user_input(pending_id, "deny")
    assert response.content == "done"
    assert not (tmp_path / "result.txt").exists()
    assert resumed.session.pending_permission_execution is None
    events, records = traces(resumed)
    assert len(records) == 1
    assert records[0].status == "completed"
    assert not records[0].incomplete
    assert len([obs for obs in records[0].observations if obs.observation_type == "agent"]) == 2


def test_ask_user_restart_restores_persisted_request_identity(runtime):
    def ask_user(question: str) -> ToolResult:
        return make_text_result(
            "ask_user",
            question,
            requires_user_input=True,
            question=question,
            request_id="persisted-ask-request",
        )

    tools = [tool_from_function(ask_user, name="ask_user")]
    handle = runtime([question("tool-call-ask")], tools=tools)
    waiting = handle.runner.run_user_turn("ask")

    assert waiting.finish_reason == "waiting_for_user_input"
    assert handle.runner.last_pending_input.id == "persisted-ask-request"
    tool_part = next(part for message in handle.session.rebuild_view().messages for part in message.parts if part.kind == "tool_call")
    assert tool_part.metadata["request_id"] == "persisted-ask-request"

    resumed = runtime([reply()], session_id=handle.session.session_id, resume=True, tools=tools)
    assert resumed.session.pending_permission_execution.request_id == "persisted-ask-request"
    assert resumed.runner.sync_pending_input_from_current_session().id == "persisted-ask-request"
    assert resumed.runner.resume_with_user_input("persisted-ask-request", "answer").content == "done"


def test_invalid_resume_request_does_not_resume_or_close_paused_trace(runtime):
    handle = runtime([question(), reply()])
    handle.runner.run_user_turn("ask")
    before, records = traces(handle)
    response = handle.runner.resume_with_user_input("wrong-request", "answer")
    after, records = traces(handle)
    assert response.finish_reason == "error"
    assert not any(event.kind in {"trace.resumed", "trace.ended"} for event in after[len(before) :])
    assert records[0].status == "waiting_for_input"
    assert not records[0].incomplete
    assert handle.runner.resume_with_user_input("ask", "answer").content == "done"


def test_new_session_does_not_reuse_previous_sessions_paused_trace(runtime):
    handle = runtime([question(), reply()])
    handle.runner.run_user_turn("ask")
    second = runtime()
    handle.runner.current_session.set_session(second.session)
    assert handle.runner.run_user_turn("new input").content == "done"
    _, first_records = traces(handle)
    _, second_records = traces(second)
    assert first_records[0].status == "waiting_for_input"
    assert len(second_records) == 1
    assert second_records[0].trace_id != first_records[0].trace_id
    assert second_records[0].status == "completed"


def test_failed_pause_write_still_resumes_durable_trace_identity(runtime, monkeypatch):
    handle = runtime([question()])
    original = handle.session.store.journal.append

    def fail_pause(kind, *args, **kwargs):
        if kind == "trace.paused":
            raise OSError("pause unavailable")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(handle.session.store.journal, "append", fail_pause)
    handle.runner.run_user_turn("ask")
    _, records = traces(handle)
    original_trace_id = records[0].trace_id
    resumed = runtime([reply()], session_id=handle.session.session_id, resume=True)
    assert resumed.runner.resume_with_user_input("ask", "answer").content == "done"
    events, records = traces(resumed)
    assert len(records) == 1
    assert records[0].trace_id == original_trace_id
    assert records[0].status == "completed"
    assert records[0].incomplete
    assert any(event.kind == "trace.resumed" and event.trace_id == original_trace_id for event in events)


def test_pending_session_write_failure_propagates_and_does_not_claim_pause(runtime, monkeypatch):
    handle = runtime([question()])

    def fail_pending(self, **kwargs):
        raise OSError("required pending persistence failed")

    monkeypatch.setattr(SessionEventWriter, "append_message_part_metadata_updated", fail_pending)
    with pytest.raises(OSError, match="required pending persistence failed"):
        handle.runner.run_user_turn("ask")
    events, records = traces(handle)
    assert not any(event.kind == "trace.paused" for event in events)
    assert records[0].status == "failed"


def test_explicit_noop_keeps_durable_pause_and_tool_result(runtime):
    handle = runtime([question()], trace_recorder=NoOpTraceRecorder())
    assert handle.runner.run_user_turn("ask").finish_reason == "waiting_for_user_input"
    resumed = runtime([reply()], session_id=handle.session.session_id, resume=True, trace_recorder=NoOpTraceRecorder())
    assert resumed.runner.resume_with_user_input("ask", "answer").content == "done"
    assert any(part.kind == "tool_result" and part.content == "answer" for message in resumed.session.rebuild_view().messages for part in message.parts)
