"""Evidence and failure boundaries, independent of runtime instrumentation."""

import json
import subprocess
from dataclasses import dataclass

import pytest

from lanscoder.journal import JournalStore
from lanscoder.observability import JournalTraceRecorder, NoOpTraceRecorder, TraceScope, project_trace, trace_context
from lanscoder.observability.git import snapshot_git
from lanscoder.storage import LansCoderPaths, PayloadRef, PayloadStore


@pytest.fixture
def setup(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    journal = JournalStore(paths, "sess_test")
    payloads = PayloadStore(paths)
    recorder = JournalTraceRecorder(journal, payloads, inline_payload_limit=100)
    return journal, payloads, recorder, TraceScope("sess_test", "brn_main")


def test_normalized_generation_and_safe_raw_evidence_are_referenced(setup):
    journal, payloads, recorder, scope = setup

    @dataclass
    class Request:
        messages: list
        temperature: float = 0.2
        max_tokens: int = 20

    class RawResponse:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {"id": "response", "content": "answer"}

    trace = recorder.start_trace(scope)
    observation = recorder.start_observation(trace, "generation", data={"normalized_request": Request([{"content": "hi"}]), "provider": "fake", "model": "m"})
    recorder.end_observation(
        observation,
        outcome="succeeded",
        data={"normalized_response": {"content": "answer"}, "provider_raw_response": RawResponse(), "usage": {"total_tokens": 5, "usage_details": {"cached_tokens": 2}}},
    )
    recorder.end_trace(trace, final_output="answer")
    events = journal.read_events()
    request = json.loads(payloads.read(PayloadRef.from_dict(events[1].data["normalized_request"]["payload_ref"])))
    response = json.loads(payloads.read(PayloadRef.from_dict(events[2].data["normalized_response"]["payload_ref"])))
    raw = json.loads(payloads.read(PayloadRef.from_dict(events[2].data["provider_raw_response"]["payload_ref"])))
    assert request["messages"] == [{"content": "hi"}]
    assert response == {"content": "answer"}
    assert raw == {"id": "response", "content": "answer"}
    summary = project_trace(events, trace, payload_store=payloads).to_summary()
    assert summary.parameters == {"temperature": 0.2, "max_tokens": 20}
    assert summary.usage_details == {"cached_tokens": 2}
    assert not summary.incomplete


@pytest.mark.parametrize("streaming", [False, True])
def test_unsafe_raw_and_stream_delta_text_are_never_labelled_raw(setup, streaming):
    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    observation = recorder.start_observation(trace, "generation", data={"normalized_request": {}, "streaming": streaming})
    data = {"normalized_response": {}, "provider_raw_response": {"unsafe": object()}}
    if streaming:
        data["stream_summary"] = {"text_delta": 2, "first_output_ms": 12, "text": "DELTA_SECRET"}
    recorder.end_observation(observation, outcome="succeeded", data=data)
    ended = journal.read_events()[-1].data
    assert "provider_raw_response" not in ended
    assert "repr" not in json.dumps(ended)
    if streaming:
        assert ended["stream_summary"] == {"text_delta": 2, "first_output_ms": 12}


def test_every_reference_is_verified_and_missing_payload_marks_incomplete(setup):
    journal, payloads, recorder, scope = setup
    trace = recorder.start_trace(scope)
    observation = recorder.start_observation(trace, "tool")
    recorder.end_observation(observation, outcome="succeeded", payload={"structured": "small"})
    recorder.end_trace(trace, final_output="done")
    events = journal.read_events()
    reference = PayloadRef.from_dict(events[2].data["payload_ref"])
    assert json.loads(payloads.read(reference)) == {"structured": "small"}
    assert not project_trace(events, trace, payload_store=payloads).incomplete
    events[2].data["payload_ref"]["size_bytes"] += 1
    assert project_trace(events, trace, payload_store=payloads).incomplete
    payloads.root.joinpath(reference.sha256).unlink()
    assert project_trace(events, trace, payload_store=payloads).incomplete


def test_unsupported_values_and_clocks_cannot_escape_recording(setup):
    journal, _, recorder, scope = setup

    class Unprintable:
        def __repr__(self):
            raise RuntimeError("must not run repr")

    trace = recorder.start_trace(scope)
    recorder.end_trace(trace, final_output=Unprintable())
    assert journal.read_events()[-1].kind == "trace.ended"
    trace = recorder.start_trace(scope)

    def broken_clock():
        raise OSError("clock failed")

    recorder._monotonic = broken_clock
    recorder.end_trace(trace, final_output="done")
    assert recorder.diagnostics


def test_index_failure_marks_every_affected_trace_without_recursive_event(setup):
    journal, _, recorder, scope = setup

    class BrokenIndex:
        def update_event(self, event):
            raise OSError("index unavailable")

    recorder.trace_index = BrokenIndex()
    traces = [recorder.start_trace(scope) for _ in range(2)]
    for trace in traces:
        recorder.end_trace(trace, final_output="done")
    events = journal.read_events()
    assert sum(event.kind == "observability.failed" for event in events) == 1
    assert all(project_trace(events, trace).incomplete for trace in traces)


def test_scope_and_reserved_fields_cannot_be_overridden(setup):
    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    with trace_context(TraceScope(scope.session_id, "brn_other"), trace_id="other", observation_id="obs_other"):
        observation = recorder.start_observation(trace, "tool", scope=TraceScope(scope.session_id, "brn_other"), data={"observation_type": "generation"})
        recorder.end_observation(observation, outcome="succeeded", data={"outcome": "failed"})
    events = journal.read_events()
    assert events[1].branch_id == "brn_main"
    assert events[1].parent_observation_id is None
    assert events[1].data["observation_type"] == "tool"
    assert events[2].data["outcome"] == "succeeded"


def test_summary_only_indexes_controlled_metadata_and_aggregates_usage(setup):
    journal, payloads, recorder, scope = setup
    trace = recorder.start_trace(scope, data={"metadata": {"team": "infra", "secret": "DROP"}, "tags": ["nightly", "x" * 65]})
    for tokens in (3, 5):
        observation = recorder.start_observation(trace, "generation", data={"normalized_request": {}})
        recorder.end_observation(
            observation, outcome="succeeded", data={"normalized_response": {}, "usage": {"total_tokens": tokens, "usage_details": {"prompt_tokens_details": {"cached_tokens": 1}}}}
        )
    recorder.end_trace(trace, final_output="done")
    summary = project_trace(journal.read_events(), trace, payload_store=payloads).to_summary()
    assert summary.total_tokens == 8
    assert summary.usage_details == {"prompt_tokens_details": {"cached_tokens": 2}}
    assert summary.metadata == {"team": "infra"}
    assert summary.tags == ("nightly",)


def test_noop_exposes_payload_and_scope_options():
    recorder = NoOpTraceRecorder()
    scope = TraceScope("sess_noop", "brn_main")
    trace = recorder.start_trace(scope)
    observation = recorder.start_observation(trace, "tool", scope=scope)
    recorder.end_observation(observation, outcome="succeeded", payload=object())
    assert recorder.record_payload(object(), force_reference=True) is None
    recorder.end_trace(trace, final_output="done", output_ref=None)


def test_git_snapshot_disables_optional_writes_and_preserves_unknown_status(monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1 if "status" in command else 0, "main\n" if "symbolic-ref" in command else "abc\n", "")

    monkeypatch.setattr("lanscoder.observability.git.subprocess.run", fake_run)
    snapshot = snapshot_git(tmp_path)
    assert snapshot.head == "abc"
    assert snapshot.dirty is None
    assert all(command[:2] == ["git", "--no-optional-locks"] for command in commands)


def test_clean_git_status_is_false_not_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr("lanscoder.observability.git.subprocess.run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "" if "status" in cmd else "main\n", ""))
    assert snapshot_git(tmp_path).dirty is False


def test_lookup_legacy_signature_is_not_called_without_pending_identity():
    from lanscoder.observability import lookup_resume_trace

    class Lookup:
        def find_resumable_trace(self, session_id, branch_id):
            return "wrong"

    assert lookup_resume_trace(Lookup(), TraceScope("sess_test", "brn_main"), tool_call_id="call1") is None


def test_lookup_legacy_signature_is_rejected_even_without_identity_filters():
    from lanscoder.observability import lookup_resume_trace

    class Lookup:
        def find_resumable_trace(self, session_id, branch_id):
            return "legacy"

    assert lookup_resume_trace(Lookup(), TraceScope("sess_test", "brn_main")) is None


def test_lookup_internal_type_error_propagates():
    from lanscoder.observability import lookup_resume_trace

    class Lookup:
        def find_resumable_trace(self, session_id, branch_id, **kwargs):
            raise TypeError("journal read failed")

    with pytest.raises(TypeError, match="journal read failed"):
        lookup_resume_trace(Lookup(), TraceScope("sess_test", "brn_main"), tool_call_id="call1")


def test_resume_lookup_uses_latest_pause_and_unique_identity(setup):
    from lanscoder.observability import JournalTraceResumeLookup, lookup_resume_trace

    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    recorder.pause_trace(trace, pending={"tool_call_id": "call1", "pending_kind": "permission"})
    recorder.pause_trace(trace, pending={"tool_call_id": "call2", "pending_kind": "ask_user"})
    lookup = JournalTraceResumeLookup(journal)
    assert lookup_resume_trace(lookup, scope, tool_call_id="call1", pending_kind="permission") is None
    assert lookup_resume_trace(lookup, scope, tool_call_id="call2", pending_kind="ask_user") == trace
    restarted = JournalTraceRecorder(journal)
    restarted.resume_trace(trace, scope=scope)
    restarted.end_trace(trace, final_output="done")
    assert lookup_resume_trace(lookup, scope, tool_call_id="call2", pending_kind="ask_user") is None
    assert not project_trace(journal.read_events(), trace).incomplete


def test_parent_and_child_projections_include_the_same_trace_link(setup):
    journal, _, recorder, scope = setup
    parent = recorder.start_trace(scope)
    child = recorder.start_trace(TraceScope(scope.session_id, "brn_child", parent))
    recorder.link_trace(parent, child)
    recorder.end_trace(parent, final_output="parent")
    recorder.end_trace(child, final_output="child")
    events = journal.read_events()
    assert events[-1].branch_id == "brn_child"
    parent_record = project_trace(events, parent)
    child_record = project_trace(events, child)
    assert parent_record.links[0]["child_trace_id"] == child
    assert child_record.links[0]["parent_trace_id"] == parent
    assert parent_record.incomplete is False
    assert child_record.incomplete is False


def test_unattributed_recovery_marks_only_execution_open_at_recovery(setup):
    journal, _, recorder, scope = setup
    complete = recorder.start_trace(scope)
    recorder.end_trace(complete, final_output="complete before recovery")
    paused = recorder.start_trace(scope)
    recorder.pause_trace(paused, pending={"tool_call_id": "pending", "pending_kind": "permission"})
    interrupted = recorder.start_trace(scope)
    journal.append("journal.recovered", {"start_byte": 100, "end_byte": 120})
    recorder.end_trace(interrupted, final_output="complete after recovery")
    later = recorder.start_trace(scope)
    recorder.end_trace(later, final_output="new trace")

    events = journal.read_events()
    assert project_trace(events, interrupted).incomplete is True
    assert project_trace(events, complete).incomplete is False
    assert project_trace(events, paused).incomplete is False
    assert project_trace(events, later).incomplete is False


@pytest.mark.parametrize("recovery_fields", [{"trace_id": "affected"}, {"data": {"trace_id": "affected"}}, {"branch_id": "brn_other"}])
def test_scoped_recovery_does_not_mark_another_running_trace(setup, recovery_fields):
    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    journal.append("journal.recovered", **recovery_fields)
    recorder.end_trace(trace, final_output="done")
    assert project_trace(journal.read_events(), trace).incomplete is False


@pytest.mark.parametrize("status", ["running", "waiting_for_input"])
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
def test_nonterminal_status_in_ended_event_is_not_accepted_as_valid_end(setup, status, outcome):
    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    journal.append("trace.ended", {"status": status, "outcome": outcome, "final_output": "done"}, trace_id=trace, branch_id=scope.branch_id)
    record = project_trace(journal.read_events(), trace)
    assert record.incomplete is True
    assert record.ended_at is None
    assert record.status == "running"


@pytest.mark.parametrize("explicit", [True, False])
def test_recovery_in_another_session_is_not_related(setup, explicit):
    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    other = journal.append("journal.recovered", session_id="sess_other", trace_id=trace if explicit else None)
    recorder.end_trace(trace, final_output="done")
    assert project_trace([*journal.read_events(), other], trace).incomplete is False


def test_recovery_tracks_unclosed_observation_even_after_trace_end(setup):
    journal, _, recorder, scope = setup
    trace = recorder.start_trace(scope)
    observation = recorder.start_observation(trace, "tool")
    recorder.end_trace(trace, final_output="done")
    journal.append("journal.recovered")
    recorder.end_observation(observation, outcome="succeeded", payload="late evidence")
    assert project_trace(journal.read_events(), trace).incomplete is True
