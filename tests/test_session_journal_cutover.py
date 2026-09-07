import json

import pytest

from lanscoder.context.events import SessionEvent
from lanscoder.context.models import utc_now_iso
from lanscoder.context.writer import SessionEventWriter
from lanscoder.context.store import JsonlSessionStore
from lanscoder.journal import JournalEnvelope
from lanscoder.session.branch import SessionBranchContext
from lanscoder.session.access import project_id_for_path as access_project_id
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.index import SessionIndex
from lanscoder.storage.paths import LansCoderPaths
from lanscoder.storage.paths import project_id_for_path as storage_project_id


def test_session_writer_persists_schema_v1_dot_kinds_and_branch_ids(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_cutover")
    writer.append_session_created(title="demo", project_id=paths.project_id, kind="primary")
    writer.append_user_message("hello")

    rows = [json.loads(line) for line in paths.session("sess_cutover").read_text().splitlines()]
    assert all(row["schema_version"] == 1 for row in rows)
    assert [row["kind"] for row in rows] == ["session.created", "message.appended"]
    root = rows[0]["data"]["root_branch_id"]
    assert rows[0]["branch_id"] == root
    assert rows[1]["branch_id"] == root


def test_legacy_store_api_translates_to_branch_stamped_schema_v1_envelopes(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_legacy",
            type="session_created",
            payload={"title": "legacy", "project_id": paths.project_id, "kind": "primary"},
            created_at="2026-09-06T00:00:00Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_user",
            session_id="sess_legacy",
            type="user_message",
            payload={
                "message_id": "msg_user",
                "parts": [{"id": "part_user", "kind": "text", "content": "hello"}],
            },
            created_at="2026-09-06T00:00:01Z",
        )
    )

    events = store.list_events("sess_legacy")
    rows = [json.loads(line) for line in paths.session("sess_legacy").read_text(encoding="utf-8").splitlines()]

    assert all(isinstance(event, JournalEnvelope) for event in events)
    assert [event.kind for event in events] == ["session.created", "message.appended"]
    assert [event.sequence for event in events] == [1, 2]
    assert [row["schema_version"] for row in rows] == [1, 1]
    assert "type" not in rows[0]
    root_branch_id = events[0].data["root_branch_id"]
    assert [event.branch_id for event in events] == [root_branch_id, root_branch_id]


def test_writer_dot_normalizes_and_branch_stamps_context_events(tmp_path):
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_context")
    writer.append_session_created(title="context")
    writer.append_message_part_metadata_updated(
        message_id="msg_1",
        part_id="part_1",
        metadata={"source": "test"},
    )
    writer.append_provider_projection_consumed(
        request_id="req_1",
        projection_fingerprint="fp_1",
        part_ids=["part_1"],
        provider="fake",
        model="fake-model",
    )
    writer.append_event("pending_context_updated", {"pending": "tool"})

    events = store.list_events("sess_context")

    assert [event.kind for event in events] == [
        "session.created",
        "message.part.metadata.updated",
        "provider.projection.consumed",
        "pending.context.updated",
    ]
    assert len({event.branch_id for event in events}) == 1
    assert all(event.branch_id is not None for event in events)


def test_writer_rejects_context_events_before_session_created(tmp_path):
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_uncreated")

    with pytest.raises(ValueError, match="session.created"):
        writer.append_user_message("must not create an orphan branch")

    assert store.list_events("sess_uncreated") == []


def test_writer_does_not_retain_root_context_when_session_created_append_fails(tmp_path, monkeypatch):
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_failed_create")
    original_append = store.journal.append

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store.journal, "append", original_append)
        raise OSError("disk full")

    monkeypatch.setattr(store.journal, "append", fail_once)

    with pytest.raises(OSError, match="disk full"):
        writer.append_session_created()

    assert writer.branch_context is None
    with pytest.raises(ValueError, match="session.created"):
        writer.append_user_message("must not use the failed root")
    assert store.list_events("sess_failed_create") == []


def test_writer_validates_supplied_branch_context_against_persisted_active_branch(tmp_path):
    store = JsonlSessionStore(tmp_path)
    root_writer = SessionEventWriter(store=store, session_id="sess_context_validation")
    root_writer.append_session_created()
    root = store.list_events("sess_context_validation")[0].branch_id
    assert root is not None

    with pytest.raises(ValueError, match="branch context"):
        SessionEventWriter(
            store=store,
            session_id="sess_context_validation",
            branch_context=SessionBranchContext("sess_context_validation", "brn_other", root),
        )

    accepted = SessionEventWriter(
        store=store,
        session_id="sess_context_validation",
        branch_context=SessionBranchContext("sess_context_validation", root, root),
    )
    accepted.append_user_message("valid context")
    assert store.list_events("sess_context_validation")[-1].branch_id == root


def test_writer_rejects_branch_context_when_journal_has_no_persisted_session_root(tmp_path):
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_orphan",
            session_id="sess_orphan_topology",
            type="user_message",
            payload={"message_id": "msg_1", "parts": []},
            created_at=utc_now_iso(),
        )
    )

    with pytest.raises(ValueError, match="session.created"):
        SessionEventWriter(
            store=store,
            session_id="sess_orphan_topology",
            branch_context=SessionBranchContext("sess_orphan_topology", "root", "root"),
        )

    writer = SessionEventWriter(store=store, session_id="sess_orphan_topology")
    with pytest.raises(ValueError, match="session.created"):
        writer.append_user_message("must not adopt an inferred root")


def test_writer_rejects_empty_persisted_root_identity_for_supplied_and_recovered_context(tmp_path):
    store = JsonlSessionStore(tmp_path)
    store.journal.append(
        "session.created",
        {"root_branch_id": ""},
        session_id="sess_empty_root_identity",
        branch_id="",
    )

    with pytest.raises(ValueError, match="branch context"):
        SessionEventWriter(
            store=store,
            session_id="sess_empty_root_identity",
            branch_context=SessionBranchContext("sess_empty_root_identity", "root", "root"),
        )

    writer = SessionEventWriter(store=store, session_id="sess_empty_root_identity")
    with pytest.raises(ValueError, match="session.created"):
        writer.append_user_message("must not inherit an empty root identity")

    assert [event.kind for event in store.list_events("sess_empty_root_identity")] == ["session.created"]


def test_session_reducer_reads_envelope_fields_without_compatibility_accessors(tmp_path, monkeypatch):
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_reduce",
            type="session_created",
            payload={"title": "reducer"},
            created_at=utc_now_iso(),
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_user",
            session_id="sess_reduce",
            type="user_message",
            payload={
                "message_id": "msg_1",
                "parts": [{"id": "part_1", "kind": "text", "content": "hello"}],
            },
            created_at=utc_now_iso(),
        )
    )

    def compatibility_accessor_used(_self):
        raise AssertionError("production reducer must use JournalEnvelope fields")

    monkeypatch.setattr(JournalEnvelope, "type", property(compatibility_accessor_used))
    monkeypatch.setattr(JournalEnvelope, "payload", property(compatibility_accessor_used))
    monkeypatch.setattr(JournalEnvelope, "created_at", property(compatibility_accessor_used))

    view = store.rebuild_session_view("sess_reduce")

    assert view.metadata["title"] == "reducer"
    assert [message.parts[0].content for message in view.messages] == ["hello"]


def test_index_does_not_forward_injected_legacy_events_to_envelope_builder(tmp_path):
    legacy_event = SessionEvent(
        id="evt_injected",
        session_id="sess_injected",
        type="session_created",
        payload={"title": "injected"},
        created_at=utc_now_iso(),
    )

    records = SessionIndex(tmp_path, journal={"sess_injected": [legacy_event]}).list_records()

    assert records[0].status == "corrupt"


def test_catalog_and_index_accept_schema_v1_and_reject_legacy_disk_records(tmp_path):
    paths = LansCoderPaths(storage_root=tmp_path)
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_catalog")
    writer.append_session_created(title="indexed", project_id=paths.project_id, kind="primary")
    writer.append_user_message("hello")

    record = SessionCatalog(tmp_path).get_session("sess_catalog")
    records = SessionCatalog(tmp_path).list_sessions()

    assert record.status == "ok"
    assert record.title == "indexed"
    assert [item.session_id for item in records] == ["sess_catalog"]

    legacy_path = paths.session("sess_legacy_disk")
    legacy_path.write_text(
        json.dumps({"id": "evt_legacy", "session_id": "sess_legacy_disk", "type": "session_created"}) + "\n",
        encoding="utf-8",
    )

    assert SessionCatalog(tmp_path).get_session("sess_legacy_disk").status == "corrupt"


def test_storage_and_access_project_identity_share_normalized_path_hash(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("lanscoder.storage.paths.Path.home", staticmethod(lambda: home))

    assert storage_project_id("~/project") == access_project_id("~/project")
