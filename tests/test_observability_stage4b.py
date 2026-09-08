"""Focused Stage 4b generation, compaction, and tool lifecycle behavior."""

from __future__ import annotations

from dataclasses import dataclass, field

from lanscoder.context.checkpoint import Checkpoint
from lanscoder.context.compaction import CompactionEvent, CompactionResult
from lanscoder.context.llm_compact import LlmCompactCandidate, LlmCompactEvent
from lanscoder.context.manager import ContextCompactMode, ContextCompactRequest, ContextWindowManager, ContextWindowTrigger
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.token_budget import ContextBudget
from lanscoder.context.writer import SessionEventWriter
from lanscoder.core.session import create_agent_session
from lanscoder.observability import JournalTraceRecorder, TraceScope, project_trace
from lanscoder.observability.models import ObservationType
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, TokenUsage, ToolCall
from lanscoder.session.branch import SessionBranchContext
from lanscoder.storage import LansCoderPaths, PayloadStore
from lanscoder.context.store import JsonlSessionStore
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
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@dataclass
class StreamingProvider(FakeProvider):
    stream_events: list[ChatStreamEvent] = field(default_factory=list)

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        for event in self.stream_events:
            yield event


def _response(content: str = "done", *, raw=None) -> ChatResponse:
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
        raw=raw,
    )


def _trace_events(handle):
    return handle.session.store.journal.read_events(handle.session.session_id)


def _budget(input_tokens: int) -> ContextBudget:
    return ContextBudget(
        context_window=32_768,
        output_reserve=4_096,
        input_capacity=27_033,
        fixed_tokens=10,
        history_tokens=max(0, input_tokens - 10),
        input_tokens=input_tokens,
        high_watermark=100,
        low_watermark=60,
        source="configured",
    )


def _message(message_id: str, content: str = "long history") -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_compaction",
        role="user",
        parts=[MessagePart(id=f"part_{message_id}", message_id=message_id, kind="text", content=content)],
    )


class _CompactionPipeline:
    def __init__(self, view: SessionView, *, after_tokens: int = 50) -> None:
        self.view = view
        self.after_tokens = after_tokens

    def compact(self, request):
        return CompactionResult(
            view=self.view,
            event=CompactionEvent(
                input_fingerprint="programmatic-fingerprint",
                before_tokens=100,
                after_tokens=self.after_tokens,
                levels_attempted=["l1"],
                stopped_at="l1",
                changed_parts=1,
            ),
        )


class _BranchSwitchingPipeline(_CompactionPipeline):
    def __init__(self, view: SessionView, store: JsonlSessionStore, branch_id: str) -> None:
        super().__init__(view, after_tokens=50)
        self.store = store
        self.branch_id = branch_id

    def compact(self, request):
        events = self.store.list_events(self.view.session_id)
        self.store.append_journal_event(
            session_id=self.view.session_id,
            kind="session.recalled",
            branch_id="branch-new",
            data={
                "new_branch_id": "branch-new",
                "parent_branch_id": self.branch_id,
                "base_sequence": events[-1].sequence,
                "excluded_target_message_id": "msg-not-used",
            },
        )
        return super().compact(request)


class _CapturingL3:
    def __init__(self, candidates) -> None:
        self.candidates = list(candidates)
        self.requests = []

    def generate_candidate(self, request):
        self.requests.append(request)
        return self.candidates.pop(0)

    def commit_candidate(self, candidate, *, runtime_state, branch_context=None):
        runtime_state.latest_checkpoint_id = candidate.checkpoint.id if candidate.checkpoint is not None else None
        return candidate.checkpoint


def test_generation_records_each_retry_with_request_response_usage_and_safe_raw(tmp_path):
    provider = FakeProvider(
        [
            ProviderError(ProviderErrorKind.RATE_LIMIT, "retry me"),
            _response(raw={"id": "provider-response", "ok": True}),
        ]
    )
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        compaction_strategy="no_compact",
    )

    assert handle.runner.run_user_turn("hello").content == "done"

    events = _trace_events(handle)
    trace_id = next(event.trace_id for event in events if event.kind == "trace.started")
    records = project_trace(
        events,
        trace_id,
        payload_store=PayloadStore(LansCoderPaths(storage_root=tmp_path / "storage")),
    )
    generations = [observation for observation in records.observations if observation.observation_type == ObservationType.GENERATION]
    assert len(generations) == 2
    assert generations[0].outcome == "failed"
    assert generations[0].data["retry_sequence"] == 0
    assert generations[1].data["retry_sequence"] == 1
    assert generations[1].data["normalized_request"]
    assert generations[1].data["normalized_response"]
    assert generations[1].data["usage"]["usage_details"]["completion_tokens_details"]["reasoning_tokens"] == 1
    raw = generations[1].data["provider_raw_response"]
    assert raw["payload_ref"] if "payload_ref" in raw else raw["id"]


def test_streaming_generation_records_first_output_time_and_only_delta_counts(tmp_path):
    response = _response()
    provider = StreamingProvider(
        [],
        stream_events=[
            ChatStreamEvent(kind="message_started"),
            ChatStreamEvent(kind="reasoning_delta", text="secret reasoning"),
            ChatStreamEvent(kind="text_delta", text="secret answer"),
            ChatStreamEvent(kind="message_completed", response=response),
        ],
    )
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        compaction_strategy="no_compact",
    )
    handle.runner.use_streaming = True

    assert handle.runner.run_user_turn("hello").content == "done"

    events = _trace_events(handle)
    ended = [event for event in events if event.kind == "observation.ended" and event.data.get("outcome") == "succeeded"]
    generation = next(event for event in ended if event.data.get("streaming"))
    summary = generation.data["stream_summary"]
    assert summary["first_output_ms"] >= 0
    assert summary["reasoning_delta"] == 1
    assert summary["text_delta"] == 1
    assert "secret" not in str(summary)


def test_denied_tool_call_has_tool_observation_and_child_event(tmp_path):
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call-denied", name="missing_tool", arguments={})],
            ),
            _response(),
        ]
    )
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[],
        compaction_strategy="no_compact",
    )

    assert handle.runner.run_user_turn("use tool").content == "done"

    events = _trace_events(handle)
    started = [event for event in events if event.kind == "observation.started" and event.data.get("observation_type") == "tool"]
    ended = [event for event in events if event.kind == "observation.ended"]
    tool = next(event for event in started if event.data.get("tool_call_id") == "call-denied")
    tool_end = next(event for event in ended if event.observation_id == tool.observation_id)
    assert tool_end.data["outcome"] == "failed"
    child_event = next(event for event in events if event.kind == "observation.started" and event.data.get("observation_type") == "event" and event.data.get("event") == "finished")
    assert child_event.parent_observation_id == tool.observation_id


def test_compaction_summarizer_accepts_scope_and_records_generation(tmp_path):
    from lanscoder.context.models import AgentMessage, MessagePart

    provider = FakeProvider([_response("## 用户请求要点\n- hi\n\n## 已给出的结论\n- done\n\n## 未完成事项\n- 无\n\n## 关键约束与偏好\n- 无")])
    paths = LansCoderPaths(storage_root=tmp_path)
    from lanscoder.journal import JournalStore

    journal = JournalStore(paths, "sess_compact")
    recorder = JournalTraceRecorder(journal, PayloadStore(paths))
    scope = TraceScope("sess_compact", "brn_main")
    trace_id = recorder.start_trace(scope)
    summarizer = ProviderLlmCompactSummarizer(provider, trace_recorder=recorder)
    summarizer.set_trace_context(scope=scope, trace_id=trace_id, parent_observation_id="obs_parent", attempt_index=1, retry_sequence=0)

    first = AgentMessage(
        id="user-1",
        session_id="sess_compact",
        role="user",
        parts=[MessagePart(id="part-1", message_id="user-1", kind="text", content="hello", metadata={"created_turn": 1})],
    )
    second = AgentMessage(
        id="user-2",
        session_id="sess_compact",
        role="user",
        parts=[MessagePart(id="part-2", message_id="user-2", kind="text", content="world", metadata={"created_turn": 20})],
    )
    result = summarizer.summarize(
        [
            first,
            second,
        ],
        current_turn=20,
        recent_turn_window=1,
    )

    assert result.summary
    started = next(event for event in journal.read_events() if event.kind == "observation.started")
    assert started.parent_observation_id == "obs_parent"
    assert started.data["operation"] == "compaction"
    assert started.data["attempt_index"] == 1


def test_manual_compaction_without_active_trace_closes_independent_root_trace(tmp_path):
    storage_root = tmp_path / "storage"
    store = JsonlSessionStore(storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_compaction")
    writer.append_session_created()
    view = SessionView(session_id="sess_compaction", messages=[_message("msg-1")])
    recorder = JournalTraceRecorder(
        store.journal,
        PayloadStore(LansCoderPaths(storage_root=storage_root)),
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=_CompactionPipeline(view),
        l3_service=None,
        trace_recorder=recorder,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_compaction"),
            budget=_budget(100),
            estimate_budget=lambda candidate: _budget(50),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
            target_tokens=60,
        )
    )

    assert result.status == "success"
    events = store.journal.read_events("sess_compaction")
    started = next(event for event in events if event.kind == "trace.started")
    ended = next(event for event in events if event.kind == "trace.ended")
    assert started.trace_id == ended.trace_id
    assert started.data["operation"] == "compaction"
    assert ended.data["status"] == "completed"
    assert ended.data["final_output"]["operation"] == "compaction"


def test_compaction_event_stays_on_captured_branch_after_active_branch_changes(tmp_path):
    storage_root = tmp_path / "storage"
    store = JsonlSessionStore(storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_compaction")
    writer.append_session_created()
    root_branch = writer.branch_context.branch_id
    assert root_branch is not None
    view = SessionView(session_id="sess_compaction", messages=[_message("msg-1")])
    manager = ContextWindowManager(
        store=store,
        pipeline=_BranchSwitchingPipeline(view, store, root_branch),
        l3_service=None,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_compaction"),
            budget=_budget(100),
            estimate_budget=lambda candidate: _budget(50),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
            target_tokens=60,
            branch_context=SessionBranchContext("sess_compaction", root_branch, root_branch),
        )
    )

    assert result.status == "success"
    events = store.list_events("sess_compaction")
    compaction = next(event for event in events if event.kind == "compaction.completed")
    assert compaction.branch_id == root_branch
    assert store.rebuild_session_view("sess_compaction").messages == []


def test_l3_fallback_preserves_trace_and_branch_context_for_retry(tmp_path):
    storage_root = tmp_path / "storage"
    store = JsonlSessionStore(storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_compaction")
    writer.append_session_created()
    view = SessionView(session_id="sess_compaction", messages=[_message("msg-1")])
    failed = LlmCompactCandidate(
        checkpoint=None,
        event=LlmCompactEvent(
            status="failed",
            source_fingerprint="source-fingerprint",
            failure_reason="prompt_too_long",
        ),
    )
    checkpoint = Checkpoint(
        id="checkpoint-1",
        session_id="sess_compaction",
        summary="summary",
        tail_start_message_id="msg-1",
        covered_until_message_id="msg-1",
        source_fingerprint="source-fingerprint",
    )
    succeeded = LlmCompactCandidate(
        checkpoint=checkpoint,
        event=LlmCompactEvent(
            status="success",
            source_fingerprint="source-fingerprint",
            checkpoint_id="checkpoint-1",
        ),
    )
    l3 = _CapturingL3([failed, succeeded])
    manager = ContextWindowManager(
        store=store,
        pipeline=_CompactionPipeline(view, after_tokens=90),
        l3_service=l3,
    )
    scope = TraceScope(
        "sess_compaction",
        "branch-main",
        parent_trace_id="trace-parent",
        parent_observation_id="observation-parent",
    )
    branch = SessionBranchContext("sess_compaction", "branch-main", "branch-root")

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_compaction"),
            budget=_budget(100),
            estimate_budget=lambda candidate: _budget(30 if candidate.checkpoints else 100),
            trigger=ContextWindowTrigger.AUTO,
            trace_scope=scope,
            trace_id="trace-parent",
            branch_context=branch,
        )
    )

    assert result.status == "success"
    assert len(l3.requests) == 2
    retry = l3.requests[1]
    assert retry.trace_scope == scope
    assert retry.trace_id == "trace-parent"
    assert retry.branch_context == branch


def test_l3_request_captures_active_branch_when_request_omits_it(tmp_path):
    storage_root = tmp_path / "storage"
    store = JsonlSessionStore(storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_compaction")
    writer.append_session_created()
    root_branch = writer.branch_context.branch_id
    assert root_branch is not None
    view = SessionView(session_id="sess_compaction", messages=[_message("msg-1")])
    checkpoint = Checkpoint(
        id="checkpoint-captured-branch",
        session_id="sess_compaction",
        summary="summary",
        tail_start_message_id="msg-1",
        covered_until_message_id="msg-1",
        source_fingerprint="source-fingerprint",
    )
    l3 = _CapturingL3(
        [
            LlmCompactCandidate(
                checkpoint=checkpoint,
                event=LlmCompactEvent(
                    status="success",
                    source_fingerprint="source-fingerprint",
                    checkpoint_id=checkpoint.id,
                ),
            )
        ]
    )
    manager = ContextWindowManager(
        store=store,
        pipeline=_CompactionPipeline(view, after_tokens=90),
        l3_service=l3,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_compaction"),
            budget=_budget(100),
            estimate_budget=lambda candidate: _budget(30 if candidate.checkpoints else 100),
            trigger=ContextWindowTrigger.MANUAL,
            mode=ContextCompactMode.MANUAL,
            target_tokens=60,
        )
    )

    assert result.status == "success"
    assert l3.requests[0].branch_context == SessionBranchContext("sess_compaction", root_branch, root_branch)


def test_permission_resume_records_decision_child_event(tmp_path):
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call-write", name="write", arguments={"path": "result.txt", "content": "new"})],
            ),
            _response(),
        ]
    )
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        tools=[create_write_tool(tmp_path)],
        compaction_strategy="no_compact",
    )

    waiting = handle.runner.run_user_turn("write a file")
    assert waiting.finish_reason == "waiting_for_user_input"
    request_id = handle.runner.last_pending_input.id
    assert handle.runner.resume_with_user_input(request_id, "allow_once").content == "done"

    events = _trace_events(handle)
    trace_id = next(event.trace_id for event in events if event.kind == "trace.started")
    records = project_trace(
        events,
        trace_id,
        payload_store=PayloadStore(LansCoderPaths(storage_root=tmp_path / "storage")),
    )
    decisions = [
        observation
        for observation in records.observations
        if observation.observation_type == ObservationType.EVENT and observation.data.get("event") == "permission_decision"
    ]
    assert len(decisions) == 1
    assert decisions[0].data["permission_request_id"] == request_id
    assert decisions[0].data["permission_decision"] == "allow"
