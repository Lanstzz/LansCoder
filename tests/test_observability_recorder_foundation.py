from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from lanscoder.storage import PayloadRef

import pytest

from lanscoder.observability import (
    JournalTraceRecorder,
    NoOpTraceRecorder,
    ObservationType,
    TraceScope,
    TraceStatus,
    get_trace_id,
    lookup_resume_trace,
    project_trace,
    trace_context,
)


@dataclass
class FakeEvent:
    sequence: int
    kind: str
    session_id: str
    trace_id: str | None
    observation_id: str | None
    parent_observation_id: str | None
    branch_id: str | None
    data: dict[str, Any]
    occurred_at: str


class FakeJournal:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[FakeEvent] = []
        self.session_id = "sess_fake"
        self.fail = fail

    def append(self, kind: str, data: dict[str, Any], **fields: Any) -> FakeEvent:
        if self.fail:
            raise OSError("journal unavailable")
        event = FakeEvent(
            sequence=len(self.events) + 1,
            kind=kind,
            session_id=fields.get("session_id", self.session_id),
            trace_id=fields.get("trace_id"),
            observation_id=fields.get("observation_id"),
            parent_observation_id=fields.get("parent_observation_id"),
            branch_id=fields.get("branch_id"),
            data=data,
            occurred_at=f"2026-09-07T00:00:0{len(self.events)}Z",
        )
        self.events.append(event)
        return event


class FakePayloadStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: dict[str, bytes] = {}
        self.fail = fail

    def put_json(self, value: Any, *, media_type: str) -> dict[str, Any]:
        if self.fail:
            raise OSError("payload unavailable")
        raw = json.dumps(value, sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.payloads[digest] = raw
        return {"sha256": digest, "media_type": media_type, "size_bytes": len(raw)}

    def read(self, reference: PayloadRef | dict[str, Any]) -> bytes:
        if isinstance(reference, PayloadRef):
            return self.payloads[reference.sha256]
        return self.payloads[reference["sha256"]]


class FailingIndex:
    def __init__(self) -> None:
        self.calls = 0

    def update_event(self, event: Any) -> None:
        self.calls += 1
        raise OSError("index unavailable")


def _scope() -> TraceScope:
    return TraceScope(session_id="sess_fake", branch_id="brn_main")


def test_noop_recorder_does_not_change_agent_result() -> None:
    recorder = NoOpTraceRecorder()
    expected = {"provider": "fake", "answer": "unchanged"}
    trace_id = recorder.start_trace(_scope())

    recorder.start_observation(trace_id, ObservationType.GENERATION)
    recorder.end_trace(trace_id, status="completed", final_output=expected)

    assert expected == {"provider": "fake", "answer": "unchanged"}


def test_lifecycle_pause_resume_observation_parent_and_branch_are_recorded() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    trace_id = recorder.start_trace(_scope())
    observation_id = recorder.start_observation(trace_id, ObservationType.AGENT, data={"step": 1})
    recorder.pause_trace(trace_id, pending={"tool_call_id": "call_1", "pending_kind": "permission"})
    recorder.resume_trace(trace_id)
    recorder.end_observation(observation_id, outcome="succeeded")
    recorder.end_trace(trace_id, status="completed", final_output="done")

    assert [event.kind for event in journal.events] == [
        "trace.started",
        "observation.started",
        "trace.paused",
        "trace.resumed",
        "observation.ended",
        "trace.ended",
    ]
    assert all(event.branch_id == "brn_main" for event in journal.events)
    assert journal.events[1].parent_observation_id is None
    assert journal.events[1].data["observation_type"] == "agent"
    assert journal.events[2].data["pending"]["tool_call_id"] == "call_1"

    record = project_trace(journal.events, trace_id)
    assert record.status is TraceStatus.COMPLETED
    assert record.incomplete is False
    assert record.final_output == "done"
    assert record.observations[0].outcome == "succeeded"


def test_parent_ids_are_inherited_and_trace_link_keeps_branch() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    parent_scope = TraceScope(session_id="sess_fake", branch_id="brn_child", parent_trace_id="trc_parent", parent_observation_id="obs_parent")
    child_id = recorder.start_trace(parent_scope, trace_id="trc_child")
    recorder.start_observation(child_id, ObservationType.TOOL)
    recorder.link_trace("trc_parent", child_id, relation="child")

    started, observation, link = journal.events
    assert started.data["parent_trace_id"] == "trc_parent"
    assert started.parent_observation_id == "obs_parent"
    assert observation.parent_observation_id == "obs_parent"
    assert link.data["parent_trace_id"] == "trc_parent"
    assert link.branch_id == "brn_child"


@pytest.mark.parametrize(
    ("status", "outcome", "kwargs"),
    [
        (TraceStatus.FAILED, "failed", {"error": {"code": "provider_error", "message": "bad response"}}),
        (TraceStatus.CANCELLED, "cancelled", {"reason": {"code": "user_cancelled"}}),
        (TraceStatus.COMPLETED, "no_generation", {"no_generation": True, "reason": {"code": "guardrail_limit"}}),
    ],
)
def test_terminal_non_successes_have_structured_reasons(status: TraceStatus, outcome: str, kwargs: dict[str, Any]) -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    trace_id = recorder.start_trace(_scope())
    recorder.end_trace(trace_id, status=status.value, **kwargs)

    ended = journal.events[-1]
    assert ended.kind == "trace.ended"
    assert ended.data["outcome"] == outcome
    assert ended.data.get("error") or ended.data.get("reason")
    record = project_trace(journal.events, trace_id)
    assert record.status is status
    assert record.incomplete is False


def test_successful_large_final_output_uses_payload_reference_and_missing_payload_is_incomplete() -> None:
    journal = FakeJournal()
    payloads = FakePayloadStore()
    recorder = JournalTraceRecorder(journal, payload_store=payloads, inline_payload_limit=4)
    trace_id = recorder.start_trace(_scope())
    recorder.end_trace(trace_id, final_output={"answer": "large"})

    ended = journal.events[-1]
    assert "output_ref" in ended.data
    record = project_trace(journal.events, trace_id, payload_store=payloads)
    assert record.output_ref == ended.data["output_ref"]
    assert record.incomplete is False
    del payloads.payloads[ended.data["output_ref"]["sha256"]]
    assert project_trace(journal.events, trace_id, payload_store=payloads).incomplete is True


def test_fail_open_storage_payload_and_index_failures_do_not_recurse() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal, payload_store=FakePayloadStore(fail=True), trace_index=FailingIndex(), inline_payload_limit=1)
    trace_id = recorder.start_trace(_scope())
    recorder.end_trace(trace_id, final_output="long output")

    assert journal.events[-1].kind == "trace.ended"
    assert [item["operation"] for item in recorder.diagnostics] == ["index", "payload", "index"]
    assert sum(event.kind == "observability.failed" for event in journal.events) == 1

    failing_journal = FakeJournal(fail=True)
    safe_recorder = JournalTraceRecorder(failing_journal)
    safe_recorder.start_trace(_scope())
    safe_recorder.end_trace("trc_missing", final_output="ignored")
    assert safe_recorder.diagnostics
    assert len(failing_journal.events) == 0


def test_unclosed_trace_is_incomplete_but_legal_pause_is_not() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    unclosed = recorder.start_trace(_scope())
    paused = recorder.start_trace(_scope())
    recorder.pause_trace(paused, pending={"pending_kind": "ask_user"})

    assert project_trace(journal.events, unclosed).incomplete is True
    paused_record = project_trace(journal.events, paused)
    assert paused_record.status is TraceStatus.WAITING_FOR_INPUT
    assert paused_record.incomplete is False


def test_unclosed_observation_marks_closed_trace_incomplete() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    trace_id = recorder.start_trace(_scope())
    recorder.start_observation(trace_id, ObservationType.TOOL)
    recorder.end_trace(trace_id, final_output="done")

    assert project_trace(journal.events, trace_id).incomplete is True


def test_binary_payload_is_stored_without_json_reencoding(tmp_path: Path) -> None:
    from lanscoder.storage import PayloadRef, PayloadStore

    journal = FakeJournal()
    payloads = PayloadStore(tmp_path)
    recorder = JournalTraceRecorder(journal, payload_store=payloads)
    trace_id = recorder.start_trace(_scope())

    reference = recorder.record_payload(b"\x00\xff", media_type="image/png", force_reference=True, trace_id=trace_id)

    assert reference is not None
    assert payloads.read(PayloadRef.from_dict(reference)) == b"\x00\xff"


def test_context_is_task_local_and_resume_lookup_uses_durable_scope() -> None:
    scope = _scope()

    class Lookup:
        def find_resumable_trace(self, session_id: str, branch_id: str, **kwargs: Any) -> str:
            assert (session_id, branch_id) == ("sess_fake", "brn_main")
            return "trc_resumed"

    assert get_trace_id() is None
    with trace_context(scope, trace_id="trc_active"):
        assert get_trace_id() == "trc_active"
        assert lookup_resume_trace(Lookup(), scope, tool_call_id="call_1") == "trc_resumed"
    assert get_trace_id() is None


def test_git_snapshot_is_read_only_for_non_repository(tmp_path: Path) -> None:
    from lanscoder.observability.git import snapshot_git

    assert snapshot_git(tmp_path).head is None


def test_small_observation_evidence_is_kept_inline() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal, inline_payload_limit=100)
    trace_id = recorder.start_trace(_scope())
    observation_id = recorder.start_observation(trace_id, ObservationType.TOOL)

    recorder.end_observation(observation_id, outcome="succeeded", payload="short output")

    ended = journal.events[-1]
    assert ended.data["payload"] == "short output"
    assert "payload_ref" not in ended.data


def test_recovery_event_only_marks_causally_related_trace_incomplete() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    affected_trace = recorder.start_trace(_scope())
    unrelated_trace = recorder.start_trace(_scope())
    recorder.end_trace(affected_trace, final_output="affected")
    recorder.end_trace(unrelated_trace, final_output="unrelated")
    journal.events.append(
        FakeEvent(
            sequence=len(journal.events) + 1,
            kind="journal.recovered",
            session_id="sess_fake",
            trace_id=affected_trace,
            observation_id=None,
            parent_observation_id=None,
            branch_id=None,
            data={"reason": "truncated tail", "trace_id": affected_trace},
            occurred_at="2026-09-07T00:00:09Z",
        )
    )

    assert project_trace(journal.events, affected_trace).incomplete is True
    assert project_trace(journal.events, unrelated_trace).incomplete is False


@pytest.mark.parametrize("status", [TraceStatus.FAILED.value, TraceStatus.CANCELLED.value])
def test_failed_or_cancelled_trace_without_reason_is_incomplete(status: str) -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    trace_id = recorder.start_trace(_scope())

    recorder.end_trace(trace_id, status=status)

    assert project_trace(journal.events, trace_id).incomplete is True


def test_model_dump_is_used_for_json_safe_evidence() -> None:
    class Model:
        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {"answer": "serialized"}

    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    trace_id = recorder.start_trace(_scope())
    recorder.end_trace(trace_id, final_output=Model())

    assert journal.events[-1].data["final_output"] == {"answer": "serialized"}


def test_trace_summary_contains_bounded_query_fields() -> None:
    journal = FakeJournal()
    recorder = JournalTraceRecorder(journal)
    trace_id = recorder.start_trace(
        _scope(),
        data={
            "provider": "fake",
            "model": "model-1",
            "metadata": {"team": "infra", "nested": {"secret": "excluded"}},
            "tags": ["nightly", "release"],
        },
    )
    recorder.start_observation(trace_id, ObservationType.TOOL, data={"tool_name": "shell"})
    recorder.end_trace(trace_id, final_output="done")

    summary = project_trace(journal.events, trace_id).to_summary()

    assert summary.provider == "fake"
    assert summary.model == "model-1"
    assert summary.tool_name == "shell"
    assert summary.metadata == {"team": "infra"}
    assert summary.tags == ("nightly", "release")
