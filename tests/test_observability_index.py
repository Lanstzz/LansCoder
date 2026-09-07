import json
import os
from pathlib import Path

import pytest

from lanscoder.journal import JournalStore
from lanscoder.observability import JournalTraceRecorder, TraceScope, TraceStatus
from lanscoder.storage import AdvisoryLock, LansCoderPaths, PayloadStore


def _make_trace(tmp_path, *, index=None):
    paths = LansCoderPaths(storage_root=tmp_path)
    journal = JournalStore(paths, "sess_index")
    recorder = JournalTraceRecorder(journal, trace_index=index)
    scope = TraceScope("sess_index", "brn_main")
    trace_id = recorder.start_trace(scope, data={"provider": "fake", "model": "m"})
    recorder.end_trace(trace_id, final_output="done")
    return paths, journal, trace_id


def _index(paths):
    import lanscoder.observability as observability

    index_type = getattr(observability, "JournalTraceIndex", None)
    assert index_type is not None, "concrete trace index is missing"
    return index_type(paths)


def test_trace_index_projects_summaries_and_updates_atomically(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    index = _index(paths)
    paths, journal, trace_id = _make_trace(tmp_path, index=index)

    summaries = index.list_summaries()

    assert [summary.trace_id for summary in summaries] == [trace_id]
    assert summaries[0].status is TraceStatus.COMPLETED
    assert summaries[0].branch_id == "brn_main"
    assert json.loads((paths.indexes / "traces.json").read_text())["traces"][trace_id]["summary"]["model"] == "m"
    assert (paths.locks / "index.lock").exists()


@pytest.mark.parametrize("index_state", ["missing", "stale", "corrupt"])
def test_trace_index_rebuilds_missing_stale_or_corrupt_index(tmp_path, index_state):
    paths, journal, trace_id = _make_trace(tmp_path)
    index = _index(paths)
    index.rebuild()
    index_path = paths.indexes / "traces.json"
    if index_state == "missing":
        index_path.unlink()
    elif index_state == "stale":
        data = json.loads(index_path.read_text())
        data["sessions"]["sess_index"] = 0
        index_path.write_text(json.dumps(data))
    else:
        index_path.write_text("{not json")

    summaries = index.list_summaries()

    assert [summary.trace_id for summary in summaries] == [trace_id]
    assert json.loads(index_path.read_text())["sessions"]["sess_index"] == 2


def test_recorder_keeps_trace_result_when_concrete_index_write_fails(tmp_path, monkeypatch):
    paths = LansCoderPaths(storage_root=tmp_path)
    index = _index(paths)
    monkeypatch.setattr(index, "_write_data", lambda data: (_ for _ in ()).throw(OSError("index unavailable")))
    journal = JournalStore(paths, "sess_index")
    recorder = JournalTraceRecorder(journal, trace_index=index)

    trace_id = recorder.start_trace(TraceScope("sess_index", "brn_main"))
    recorder.end_trace(trace_id, final_output="still returned")

    assert journal.read_events()[-1].kind == "trace.ended"
    assert any(item["operation"] == "index" for item in recorder.diagnostics)


def test_trace_index_verifies_existing_payloads_without_injection(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    index = _index(paths)
    journal = JournalStore(paths, "sess_index")
    recorder = JournalTraceRecorder(journal, PayloadStore(paths), trace_index=index, inline_payload_limit=1)
    trace_id = recorder.start_trace(TraceScope("sess_index", "brn_main"))
    recorder.end_trace(trace_id, final_output="large output")

    assert index.list_summaries()[0].incomplete is False


def test_default_journal_recorder_materializes_trace_index(tmp_path):
    paths, _, trace = _make_trace(tmp_path)
    data = json.loads((paths.indexes / "traces.json").read_text())
    assert data["traces"][trace]["summary"]["status"] == "completed"


@pytest.mark.parametrize("damage", ["summary", "missing_trace", "extra_trace", "watermark"])
def test_trace_index_rebuilds_semantically_damaged_cache(tmp_path, damage):
    paths, _, trace = _make_trace(tmp_path)
    index = _index(paths)
    index.rebuild()
    data = json.loads(index.path.read_text())
    if damage == "summary":
        data["traces"][trace]["summary"]["incomplete"] = "false"
        data["traces"][trace]["summary"]["metadata"] = {"provider_raw": "must not be indexed"}
    elif damage == "missing_trace":
        del data["traces"][trace]
    elif damage == "extra_trace":
        data["traces"]["bogus"] = data["traces"][trace]
    else:
        data["sessions"]["sess_index"] = 999
    index.path.write_text(json.dumps(data))

    summaries = index.list_summaries()
    assert [(item.trace_id, item.incomplete, item.metadata) for item in summaries] == [(trace, False, {})]
    assert json.loads(index.path.read_text())["sessions"]["sess_index"] == 2


def test_trace_index_refreshes_missing_payload_even_without_new_event(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    payloads = PayloadStore(paths)
    index = _index(paths)
    recorder = JournalTraceRecorder(JournalStore(paths, "sess_index"), payloads, index, inline_payload_limit=1)
    trace = recorder.start_trace(TraceScope("sess_index", "brn_main"))
    recorder.end_trace(trace, final_output="output")
    assert index.list_summaries()[0].incomplete is False
    for payload in paths.payloads.iterdir():
        payload.unlink()
    assert index.list_summaries()[0].incomplete is True


@pytest.mark.parametrize("operation", ["rebuild", "update"])
def test_stale_snapshot_never_overwrites_concurrent_index_update(tmp_path, monkeypatch, operation):
    paths, journal, trace = _make_trace(tmp_path)
    index = _index(paths)
    index.rebuild()
    original_load = index._load_session_events
    interleaved = False

    def load_with_interleaving(session_id):
        nonlocal interleaved
        snapshot = original_load(session_id)
        if not interleaved:
            interleaved = True
            event = journal.append("observability.failed", {"operation": "payload"}, trace_id=trace, branch_id="brn_main")
            _index(paths).update_event(event)
        return snapshot

    monkeypatch.setattr(index, "_load_session_events", load_with_interleaving)
    if operation == "rebuild":
        index.rebuild()
    else:
        index.update_event(journal.read_events()[-1])
    data = json.loads(index.path.read_text())
    assert data["sessions"]["sess_index"] == 3
    assert data["traces"][trace]["summary"]["incomplete"] is True


def test_index_atomic_write_uses_independent_lock_and_syncs_before_replace(tmp_path, monkeypatch):
    paths, journal, trace = _make_trace(tmp_path)
    held = []
    synced = []
    replacements = []
    real_acquire, real_release = AdvisoryLock.acquire, AdvisoryLock.release
    real_fsync, real_replace = os.fsync, os.replace

    def acquire(lock):
        assert not held, "session/index locks must not nest"
        result = real_acquire(lock)
        held.append(lock.path)
        return result

    def release(lock):
        held.pop()
        real_release(lock)

    def fsync(descriptor):
        synced.append(descriptor)
        real_fsync(descriptor)

    def replace(source, destination):
        assert held == [paths.index_lock]
        assert synced
        assert Path(source).parent == paths.indexes
        assert json.loads(Path(source).read_text())["traces"][trace]["summary"]["status"] == "completed"
        replacements.append(destination)
        real_replace(source, destination)

    monkeypatch.setattr(AdvisoryLock, "acquire", acquire)
    monkeypatch.setattr(AdvisoryLock, "release", release)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    _index(paths).rebuild()
    assert replacements == [paths.indexes / "traces.json"]
    assert not list(paths.indexes.glob(".traces.*.tmp"))


def test_failed_required_journal_read_preserves_previous_index(tmp_path, monkeypatch):
    paths, _, _ = _make_trace(tmp_path)
    index = _index(paths)
    index.rebuild()
    before = index.path.read_bytes()

    def failed_read(session_id):
        raise TypeError("required read failed")

    monkeypatch.setattr(index, "_load_session_events", failed_read)
    with pytest.raises(TypeError, match="required read failed"):
        index.rebuild()
    assert index.path.read_bytes() == before
