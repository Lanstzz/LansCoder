"""Tests for /recall truncation and handler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanscoder.context.events import SessionEvent
from lanscoder.context.store import JsonlSessionStore


def _write_session_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _make_event(session_id: str, event_id: str, event_type: str, payload: dict) -> dict:
    return {
        "id": event_id,
        "session_id": session_id,
        "type": event_type,
        "payload": payload,
        "created_at": "2026-08-18T12:00:00Z",
    }


def _make_user_message_event(session_id: str, message_id: str, content: str, turn: int) -> dict:
    return _make_event(
        session_id,
        f"evt_{message_id}",
        "user_message",
        {
            "message_id": message_id,
            "parts": [
                {
                    "id": f"part_{message_id}",
                    "message_id": message_id,
                    "kind": "text",
                    "content": content,
                    "metadata": {"created_turn": turn, "turn_id": turn},
                }
            ],
            "metadata": {},
        },
    )


def _make_assistant_message_event(session_id: str, message_id: str, content: str) -> dict:
    return _make_event(
        session_id,
        f"evt_{message_id}",
        "assistant_message",
        {
            "message_id": message_id,
            "parts": [
                {
                    "id": f"part_{message_id}",
                    "message_id": message_id,
                    "kind": "text",
                    "content": content,
                    "metadata": {},
                }
            ],
            "metadata": {"provider": "test", "model": "test", "finish_reason": "stop"},
        },
    )


class TestTruncateBeforeMessage:
    def test_truncate_to_specific_message(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi there"),
            _make_user_message_event(sid, "msg_03", "do something", 2),
            _make_assistant_message_event(sid, "msg_04", "ok done"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        retained = store.truncate_before_message(sid, "msg_03")

        assert retained == 3  # session_created + msg_01 + msg_02
        remaining = store.list_events(sid)
        assert len(remaining) == 3
        assert remaining[0].type == "session_created"
        assert remaining[1].type == "user_message"
        assert remaining[1].payload["message_id"] == "msg_01"
        assert remaining[2].type == "assistant_message"

    def test_truncate_to_first_turn(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi there"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        retained = store.truncate_before_message(sid, "msg_01")

        assert retained == 1  # only session_created
        remaining = store.list_events(sid)
        assert len(remaining) == 1
        assert remaining[0].type == "session_created"

    def test_truncate_preserves_session_created(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "turn 1", 1),
            _make_assistant_message_event(sid, "msg_02", "response 1"),
            _make_user_message_event(sid, "msg_03", "turn 2", 2),
            _make_assistant_message_event(sid, "msg_04", "response 2"),
            _make_user_message_event(sid, "msg_05", "turn 3", 3),
            _make_assistant_message_event(sid, "msg_06", "response 3"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        retained = store.truncate_before_message(sid, "msg_05")

        remaining = store.list_events(sid)
        assert remaining[0].type == "session_created"
        assert remaining[0].payload["session_id"] == sid
        assert remaining[-1].type == "assistant_message"
        assert remaining[-1].payload["message_id"] == "msg_04"

    def test_truncate_atomic_on_error(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        original_content = store._session_path(sid).read_text()

        with pytest.raises(ValueError, match="message_id not found"):
            store.truncate_before_message(sid, "msg_nonexistent")

        assert store._session_path(sid).read_text() == original_content

    def test_truncate_nonexistent_session(self, tmp_path):
        store = JsonlSessionStore(tmp_path)

        with pytest.raises(FileNotFoundError):
            store.truncate_before_message("sess_nonexistent", "msg_01")

    def test_truncate_rejects_non_user_message(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        with pytest.raises(ValueError, match="is not a user_message"):
            store.truncate_before_message(sid, "msg_02")


class TestSessionIndexRebuildSession:
    def test_rebuild_session_updates_index(self, tmp_path):
        from lanscoder.session.index import SessionIndex

        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi there"),
            _make_user_message_event(sid, "msg_03", "do something", 2),
            _make_assistant_message_event(sid, "msg_04", "ok done"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        index = SessionIndex(tmp_path)
        index.rebuild()
        records_before = index.list_records()
        assert len(records_before) == 1
        assert records_before[0].user_turn_count == 2

        # Truncate to turn 1
        store.truncate_before_message(sid, "msg_03")
        # Rebuild just this session's index entry
        index.rebuild_session(sid)

        records_after = index.list_records()
        assert len(records_after) == 1
        assert records_after[0].user_turn_count == 1
        assert records_after[0].message_count == 2  # msg_01 + msg_02