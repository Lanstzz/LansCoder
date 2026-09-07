import json
import os
from pathlib import Path

import pytest

from lanscoder.context.events import SessionEvent
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.app.session_commands import SessionCommandHandler
from lanscoder.session.access import (
    SessionAccessDescriptor,
    SessionAccessError,
    SessionAccessPolicy,
    project_id_for_path,
)
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.fork import ForkSessionService
from lanscoder.session.resume import ResumeService
from lanscoder.session.index import SessionIndex
from lanscoder.storage import LansCoderPaths


def _primary_writer(tmp_path: Path, session_id: str, *, title: str = "session") -> tuple[JsonlSessionStore, SessionEventWriter]:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id=session_id)
    writer.append_session_created(title=title, project_id=paths.project_id, kind="primary")
    return store, writer


def test_session_index_uses_independent_lock_atomic_replace_and_watermark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, writer = _primary_writer(tmp_path, "sess_index")
    writer.append_user_message("hello")
    index_module = __import__("lanscoder.session.index", fromlist=["SessionIndex"])
    replacements: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(source: str, destination: str) -> None:
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(index_module, "os", os, raising=False)
    monkeypatch.setattr(os, "replace", recording_replace)
    SessionIndex(store.root).update_event(store.list_events("sess_index")[-1])

    index_path = store.root / "indexes" / "sessions.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    assert (store.root / "locks" / "index.lock").exists()
    assert data["sessions"]["sess_index"]["last_sequence"] == 2
    assert replacements and replacements[-1][1] == index_path
    assert not list(index_path.parent.glob("*.tmp"))


@pytest.mark.parametrize("index_contents", [None, "{not json}", '{"version": 1, "sessions": {}}'])
def test_session_index_rebuilds_missing_stale_or_corrupt_index_from_journals(
    tmp_path: Path, index_contents: str | None
) -> None:
    store, writer = _primary_writer(tmp_path, "sess_rebuild", title="Rebuilt")
    writer.append_user_message("from journal")
    index_path = store.root / "indexes" / "sessions.json"
    if index_contents is None:
        index_path.unlink()
    else:
        index_path.write_text(index_contents, encoding="utf-8")

    records = SessionIndex(store.root).list_records()

    assert [(record.session_id, record.title) for record in records] == [("sess_rebuild", "Rebuilt")]
    assert json.loads(index_path.read_text(encoding="utf-8"))["sessions"]["sess_rebuild"]["last_sequence"] == 2


def test_session_index_rebuilds_when_watermark_is_stale_and_matches_deleted_index(tmp_path: Path) -> None:
    store, writer = _primary_writer(tmp_path, "sess_stale", title="Stale")
    writer.append_user_message("journal truth")
    index_path = store.root / "indexes" / "sessions.json"
    indexed = json.loads(index_path.read_text(encoding="utf-8"))
    indexed["sessions"]["sess_stale"]["last_sequence"] = 1
    index_path.write_text(json.dumps(indexed), encoding="utf-8")

    rebuilt = SessionIndex(store.root).list_records()
    index_path.unlink()
    deleted_index = SessionIndex(store.root).list_records()

    assert [(record.session_id, record.message_count) for record in rebuilt] == [("sess_stale", 1)]
    assert [(record.session_id, record.message_count) for record in deleted_index] == [("sess_stale", 1)]


def test_session_index_rebuilds_when_a_record_field_is_malformed(tmp_path: Path) -> None:
    store, writer = _primary_writer(tmp_path, "sess_malformed_index", title="Journal truth")
    writer.append_user_message("durable message")
    index_path = store.root / "indexes" / "sessions.json"
    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    index_data["sessions"]["sess_malformed_index"]["message_count"] = "not an integer"
    index_path.write_text(json.dumps(index_data), encoding="utf-8")

    records = SessionIndex(store.root).list_records()

    assert [(record.session_id, record.message_count, record.status) for record in records] == [
        ("sess_malformed_index", 1, "ok")
    ]


def test_session_index_rebuilds_malformed_json_when_no_journals_exist(tmp_path: Path) -> None:
    index_path = tmp_path / "indexes" / "sessions.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{not json", encoding="utf-8")

    records = SessionIndex(tmp_path).list_records()

    assert records == []
    assert json.loads(index_path.read_text(encoding="utf-8")) == {"version": 1, "sessions": {}}


def test_session_index_rebuilds_to_corrupt_record_when_journal_read_fails(tmp_path: Path) -> None:
    store, _ = _primary_writer(tmp_path, "sess_unavailable", title="Available")

    class FlakyJournal:
        def __init__(self) -> None:
            self.failed = False

        def session_ids(self) -> list[str]:
            return ["sess_unavailable"]

        def read_events(self, session_id: str):
            if self.failed:
                raise OSError("journal temporarily unavailable")
            return store.list_events(session_id)

    journal = FlakyJournal()
    index = SessionIndex(store.root, journal=journal)
    index.rebuild()
    journal.failed = True

    records = index.list_records()

    assert len(records) == 1
    assert records[0].status == "corrupt"
    assert "temporarily unavailable" in (records[0].error or "")


def test_session_catalog_lists_only_current_project_primary_sessions(tmp_path: Path) -> None:
    store, _ = _primary_writer(tmp_path, "sess_current", title="Current")
    other_project = tmp_path / "other"
    other_writer = SessionEventWriter(store=store, session_id="sess_other")
    other_writer.append_session_created(project_id=project_id_for_path(other_project), kind="primary", title="Other")
    child_writer = SessionEventWriter(store=store, session_id="sess_child")
    child_writer.append_session_created(project_id=project_id_for_path(tmp_path), kind="subagent", title="Child")

    records = SessionCatalog(store.root, project_id=project_id_for_path(tmp_path)).list_sessions()

    assert [record.session_id for record in records] == ["sess_current"]


def test_index_failure_does_not_rewrite_persisted_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, writer = _primary_writer(tmp_path, "sess_index_failure")
    monkeypatch.setattr(SessionIndex, "_write_data", lambda self, data: (_ for _ in ()).throw(OSError("index unavailable")))

    writer.append_user_message("still durable")

    events = store.list_events("sess_index_failure")
    assert [event.data.get("role") for event in events if event.kind == "message.appended"] == ["user"]


def test_session_catalog_filters_current_project_primary_but_index_keeps_subagent_evidence(tmp_path: Path) -> None:
    project_id = project_id_for_path(tmp_path)
    other_project = project_id_for_path(tmp_path / "other")
    journal = {
        "sess_primary": [
            _envelope(1, "sess_primary", "session.created", project_id=project_id, session_kind="primary", title="primary"),
        ],
        "sess_other": [_envelope(1, "sess_other", "session.created", project_id=other_project, session_kind="primary")],
        "sess_child": [_envelope(1, "sess_child", "session.created", project_id=project_id, session_kind="subagent")],
    }
    index = SessionIndex(tmp_path, journal=journal, project_id=project_id)
    index.rebuild()

    assert [record.session_id for record in index.list_records(project_id=project_id, kind="subagent")] == ["sess_child"]
    assert [record.session_id for record in index.list_records(kind="subagent")] == ["sess_child"]


@pytest.mark.parametrize("kind", ["subagent", "background"])
def test_session_command_rejects_non_primary_user_targets(tmp_path: Path, kind: str) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    for session_id, project_id, session_kind in (
        ("sess_other_project", project_id_for_path(tmp_path / "other"), "primary"),
        ("sess_non_primary", paths.project_id, kind),
    ):
        SessionEventWriter(store=store, session_id=session_id).append_session_created(
            project_id=project_id,
            kind=session_kind,
        )
    catalog = SessionCatalog(store.root)
    handler = SessionCommandHandler(
        catalog=catalog,
        access_policy=SessionAccessPolicy(tmp_path, journal=catalog),
    )

    for session_id in ("sess_other_project", "sess_non_primary"):
        result = handler.handle(f"/session {session_id}")
        assert result.handled is True
        assert "Session error:" in result.output
        assert "another project" in result.output or "not a primary" in result.output


@pytest.mark.parametrize("kind", ["subagent", "background"])
def test_session_access_rejects_non_primary_user_targets(tmp_path: Path, kind: str) -> None:
    project_id = project_id_for_path(tmp_path)
    policy = SessionAccessPolicy(
        tmp_path,
        journal={
            "sess_other": SessionAccessDescriptor("sess_other", "other", "primary"),
            "sess_non_primary": SessionAccessDescriptor("sess_non_primary", project_id, kind),
            "sess_primary": SessionAccessDescriptor("sess_primary", project_id, "primary"),
        },
    )

    with pytest.raises(SessionAccessError, match="primary|another project"):
        policy.open_primary("sess_other")
    with pytest.raises(SessionAccessError, match="primary"):
        policy.open_primary("sess_non_primary")
    with pytest.raises(SessionAccessError, match="already exists"):
        policy.create_primary("sess_primary")


def test_resume_and_fork_reject_other_project_and_subagent_at_service_boundary(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    for session_id, project_id, kind in (
        ("sess_other", project_id_for_path(tmp_path / "other"), "primary"),
        ("sess_child", paths.project_id, "subagent"),
        ("sess_background", paths.project_id, "background"),
    ):
        writer = SessionEventWriter(store=store, session_id=session_id)
        writer.append_session_created(project_id=project_id, kind=kind)

    resume = ResumeService(store=store, project_root=tmp_path, paths=paths)
    fork = ForkSessionService(store=store, project_root=tmp_path, paths=paths)
    for session_id in ("sess_other", "sess_child", "sess_background"):
        with pytest.raises(SessionAccessError, match="primary|another project"):
            resume.resume(session_id)
        with pytest.raises(SessionAccessError, match="primary|another project"):
            fork.fork(session_id)


def test_fork_copies_only_active_projection_with_fresh_ids_and_retrievable_archive(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store, writer = _primary_writer(tmp_path, "sess_source", title="Source")
    first_message_id = writer.append_user_message("visible before recall")
    root_branch = writer.branch_context.branch_id
    child_branch = "brn_recalled"
    store.append_journal_event(
        session_id="sess_source",
        kind="session.recalled",
        branch_id=child_branch,
        data={
            "new_branch_id": child_branch,
            "parent_branch_id": root_branch,
            "base_sequence": 2,
            "excluded_target_message_id": "unused",
        },
    )
    second_message_id = SessionEventWriter(store=store, session_id="sess_source").append_user_message("active after recall")
    checkpoint = {
        "id": "ckpt_source",
        "session_id": "sess_source",
        "summary": "checkpoint",
        "tail_start_message_id": second_message_id,
        "covered_until_message_id": second_message_id,
        "source_fingerprint": "fingerprint",
        "strategy_version": "v1",
    }
    store.append_event(SessionEvent(id="evt_checkpoint", session_id="sess_source", type="checkpoint_created", payload=checkpoint))
    archive_dir = paths.archives / "sess_source"
    archive_dir.mkdir(parents=True)
    (archive_dir / "ar_saved.txt").write_text("archived evidence", encoding="utf-8")
    (archive_dir / "ar_saved.json").write_text(json.dumps({"archive_id": "ar_saved"}), encoding="utf-8")

    result = ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_source", title="Fork")
    forked_events = store.list_events(result.session.session_id)
    forked_view = result.session.rebuild_view()

    assert forked_events[0].kind == "session.created"
    assert forked_events[0].branch_id == forked_events[0].data["root_branch_id"]
    assert forked_events[0].branch_id != root_branch
    assert not any(event.kind == "session.recalled" for event in forked_events)
    assert [message.parts[0].content for message in forked_view.messages] == ["visible before recall", "active after recall"]
    assert [message.id for message in forked_view.messages] != [first_message_id, second_message_id]
    forked_checkpoint = forked_view.checkpoints[0]
    assert forked_checkpoint.id != "ckpt_source"
    assert forked_checkpoint.session_id == result.session.session_id
    assert all(event.session_id == result.session.session_id for event in forked_events)
    assert (paths.archives / result.session.session_id / "ar_saved.txt").read_text(encoding="utf-8") == "archived evidence"


def test_fork_requested_title_is_final_after_source_metadata_updates(tmp_path: Path) -> None:
    store, writer = _primary_writer(tmp_path, "sess_title_source", title="Initial")
    writer.append_session_metadata_updated(title="Later source title")

    result = ForkSessionService(store=store, project_root=tmp_path).fork("sess_title_source", title="Requested fork title")

    assert result.record.title == "Requested fork title"
    assert result.session.rebuild_view().metadata["title"] == "Requested fork title"


def test_fork_allocates_and_consistently_remaps_tool_call_ids(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store, writer = _primary_writer(tmp_path, "sess_tool_source")
    branch_id = writer.branch_context.branch_id
    store.append_journal_event(
        session_id="sess_tool_source",
        kind="message.appended",
        branch_id=branch_id,
        data={
            "role": "assistant",
            "message_id": "msg_tool_source",
            "parts": [
                {
                    "id": "part_tool_source",
                    "message_id": "msg_tool_source",
                    "kind": "tool_call",
                    "content": "",
                    "metadata": {"tool_call_id": "call_source", "tool_name": "echo"},
                }
            ],
        },
    )
    store.append_journal_event(
        session_id="sess_tool_source",
        kind="message.appended",
        branch_id=branch_id,
        data={
            "role": "tool",
            "message_id": "msg_result_source",
            "parts": [
                {
                    "id": "part_result_source",
                    "message_id": "msg_result_source",
                    "kind": "tool_result",
                    "content": "ok",
                    "metadata": {"tool_call_id": "call_source", "tool_name": "echo"},
                }
            ],
        },
    )

    result = ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_tool_source")
    messages = result.session.rebuild_view().messages
    tool_call_id = next(
        part.metadata["tool_call_id"]
        for message in messages
        for part in message.parts
        if part.kind == "tool_call"
    )
    tool_result_id = next(
        part.metadata["tool_call_id"]
        for message in messages
        for part in message.parts
        if part.kind == "tool_result"
    )

    assert tool_call_id == tool_result_id
    assert tool_call_id != "call_source"


def test_catalog_exists_does_not_accept_legacy_dictionary_records(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sess_legacy.jsonl").write_text(
        '{"id":"evt","session_id":"sess_legacy","type":"session_created","payload":{}}\n',
        encoding="utf-8",
    )

    assert SessionCatalog(tmp_path).exists("sess_legacy") is False
    assert SessionCatalog(tmp_path).get_session("sess_legacy").status == "corrupt"


def _envelope(sequence: int, session_id: str, event_kind: str, **data):
    from lanscoder.journal.models import JournalEnvelope

    branch_id = data.pop("root_branch_id", "root")
    session_kind = data.pop("session_kind", None)
    if session_kind is not None:
        data["kind"] = session_kind
    if event_kind == "session.created":
        data["root_branch_id"] = branch_id
    return JournalEnvelope.create(sequence=sequence, kind=event_kind, session_id=session_id, branch_id=branch_id, data=data)
