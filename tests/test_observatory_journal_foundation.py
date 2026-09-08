from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from lanscoder.journal import JournalCorruptError, JournalStore
from lanscoder.storage import LansCoderPaths, PayloadIntegrityError, PayloadStore


def test_paths_default_and_project_identity_are_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    project = tmp_path / "project"
    paths = LansCoderPaths(project_root=project)

    assert paths.storage_root == tmp_path / "home" / ".lanscoder"
    assert paths.project_state.parent == paths.storage_root / "projects"
    assert len(paths.project_id) == 64
    assert not (project / ".lanscoder").exists()


def test_payload_is_content_addressed_atomic_and_verified(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path)
    store = PayloadStore(paths)
    content = b"payload content"
    reference = store.put(content, media_type="text/plain")

    assert reference.sha256 == hashlib.sha256(content).hexdigest()
    assert (tmp_path / "payloads" / reference.sha256).read_bytes() == content
    assert store.read(reference) == content
    (tmp_path / "payloads" / reference.sha256).write_bytes(b"tampered")
    with pytest.raises(PayloadIntegrityError):
        store.read(reference)


def _append_from_process(root: str, session_id: str, count: int) -> None:
    store = JournalStore(LansCoderPaths(storage_root=root), session_id)
    for index in range(count):
        store.append("test.event", {"index": index})


def test_cross_process_append_assigns_monotonic_sequences(tmp_path: Path) -> None:
    session_id = "sess_parallel"
    processes = [multiprocessing.Process(target=_append_from_process, args=(str(tmp_path), session_id, 8)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    events = JournalStore(LansCoderPaths(storage_root=tmp_path), session_id).read_events()
    assert [event.sequence for event in events] == list(range(1, 25))


def test_incomplete_tail_is_preserved_and_recorded(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JournalStore(paths, "sess_tail")
    store.append("session.created", {"root_branch_id": "brn_root"}, branch_id="brn_root")
    path = paths.session("sess_tail")
    tail = b'{"schema_version":1,"sequence":2'
    with path.open("ab") as handle:
        handle.write(tail)

    events = store.read_events()
    assert [event.kind for event in events] == ["session.created", "journal.recovered"]
    recovery = events[-1].data
    assert recovery["tail_sha256"] == hashlib.sha256(tail).hexdigest()
    assert Path(recovery["evidence_path"]).read_bytes() == tail
    assert path.read_bytes().endswith(b"\n")


def test_complete_tail_without_newline_is_retained(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JournalStore(paths, "sess_complete_tail")
    store.append("one", {})
    path = paths.session("sess_complete_tail")
    records = path.read_bytes().splitlines()
    path.write_bytes(b"\n".join(records[:-1] + [records[-1].rstrip(b"\n")]))

    events = store.read_events()
    assert [event.sequence for event in events] == [1]
    assert events[0].kind == "one"
    assert path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: {**record, "sequence": 3},
        lambda record: {**record, "schema_version": 99},
    ],
)
def test_middle_or_schema_corruption_is_not_silently_repaired(tmp_path: Path, mutation) -> None:
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JournalStore(paths, "sess_corrupt")
    store.append("one", {})
    store.append("two", {})
    path = paths.session("sess_corrupt")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0] = mutation(records[0])
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    with pytest.raises(JournalCorruptError):
        store.read_events()
    assert len(path.read_text().splitlines()) == 2
