import json
from pathlib import Path

from lanscoder.context.events import SessionEvent
from lanscoder.context.store import JsonlSessionStore
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.index import SessionIndex


def test_store_append_event_updates_session_index(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)

    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_test",
            type="session_created",
            payload={"title": "Demo"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_user",
            session_id="sess_test",
            type="user_message",
            payload={
                "message_id": "msg_user",
                "parts": [{"id": "part_user", "kind": "text", "content": "hello"}],
            },
            created_at="2026-06-01T00:00:01Z",
        )
    )

    index_path = tmp_path / "session_index.json"
    assert index_path.exists()
    data = json.loads(index_path.read_text(encoding="utf-8"))
    record = data["sessions"]["sess_test"]
    assert record["title"] == "Demo"
    assert record["updated_at"] == "2026-06-01T00:00:01Z"
    assert record["message_count"] == 1
    assert record["user_turn_count"] == 1
    assert record["latest_user_input"] == "hello"


def test_catalog_lists_sessions_from_index_without_reading_jsonl(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_test",
            type="session_created",
            payload={"title": "Indexed"},
            created_at="2026-06-01T00:00:00Z",
        )
    )

    records = SessionCatalog(tmp_path).list_sessions()

    assert [record.session_id for record in records] == ["sess_test"]
    assert records[0].title == "Indexed"


def test_session_index_rebuilds_missing_index_from_existing_jsonl(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_old",
            type="session_created",
            payload={"title": "Old"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    (tmp_path / "session_index.json").unlink()

    records = SessionIndex(tmp_path).list_records()

    assert [record.session_id for record in records] == ["sess_old"]
    assert (tmp_path / "session_index.json").exists()


def test_prune_empty_removes_zero_turn_sessions(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)

    # Session A: 2 user turns — should be kept
    store.append_event(
        SessionEvent(
            id="evt_a1",
            session_id="sess_a",
            type="session_created",
            payload={"session_id": "sess_a", "title": "A"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_a2",
            session_id="sess_a",
            type="user_message",
            payload={"message_id": "ma1", "parts": [{"id": "pa1", "kind": "text", "content": "turn 1"}]},
            created_at="2026-06-01T00:00:01Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_a3",
            session_id="sess_a",
            type="assistant_message",
            payload={"message_id": "ma2", "parts": [{"id": "pa2", "kind": "text", "content": "resp 1"}], "metadata": {}},
            created_at="2026-06-01T00:00:02Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_a4",
            session_id="sess_a",
            type="user_message",
            payload={"message_id": "ma3", "parts": [{"id": "pa3", "kind": "text", "content": "turn 2"}]},
            created_at="2026-06-01T00:00:03Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_a5",
            session_id="sess_a",
            type="assistant_message",
            payload={"message_id": "ma4", "parts": [{"id": "pa4", "kind": "text", "content": "resp 2"}], "metadata": {}},
            created_at="2026-06-01T00:00:04Z",
        )
    )

    # Session B: 0 user turns (session_created only) — should be pruned
    store.append_event(
        SessionEvent(
            id="evt_b1",
            session_id="sess_b",
            type="session_created",
            payload={"session_id": "sess_b", "title": "B"},
            created_at="2026-06-01T00:00:05Z",
        )
    )

    # Session C: 1 user turn — should be kept
    store.append_event(
        SessionEvent(
            id="evt_c1",
            session_id="sess_c",
            type="session_created",
            payload={"session_id": "sess_c", "title": "C"},
            created_at="2026-06-01T00:00:06Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_c2",
            session_id="sess_c",
            type="user_message",
            payload={"message_id": "mc1", "parts": [{"id": "pc1", "kind": "text", "content": "only turn"}]},
            created_at="2026-06-01T00:00:07Z",
        )
    )

    index = SessionIndex(tmp_path)
    index.prune_empty()

    # Verify sess_b JSONL is deleted
    assert not (tmp_path / "sessions" / "sess_b.jsonl").exists()

    # Verify sess_a and sess_c JSONL still exist
    assert (tmp_path / "sessions" / "sess_a.jsonl").exists()
    assert (tmp_path / "sessions" / "sess_c.jsonl").exists()

    # Verify index only has sess_a and sess_c
    records = index.list_records()
    ids = {record.session_id for record in records}
    assert ids == {"sess_a", "sess_c"}


def test_prune_empty_respects_exclude(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)

    # Session A: 0 user turns — would be pruned, but excluded
    store.append_event(
        SessionEvent(
            id="evt_a1",
            session_id="sess_a",
            type="session_created",
            payload={"session_id": "sess_a"},
            created_at="2026-06-01T00:00:00Z",
        )
    )

    # Session B: 0 user turns — should be pruned (not excluded)
    store.append_event(
        SessionEvent(
            id="evt_b1",
            session_id="sess_b",
            type="session_created",
            payload={"session_id": "sess_b"},
            created_at="2026-06-01T00:00:01Z",
        )
    )

    index = SessionIndex(tmp_path)
    index.prune_empty(exclude={"sess_a"})

    # sess_a is excluded, so its JSONL still exists
    assert (tmp_path / "sessions" / "sess_a.jsonl").exists()
    # sess_b is not excluded, so it's pruned
    assert not (tmp_path / "sessions" / "sess_b.jsonl").exists()
