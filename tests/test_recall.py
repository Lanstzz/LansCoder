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


# ---------------------------------------------------------------------------
# Task 3: RecallCommandHandler tests
# ---------------------------------------------------------------------------


from lanscoder.app.recall_commands import RecallCommandHandler
from lanscoder.app.commands import CommandResult
from lanscoder.agent.session import AgentSession
from lanscoder.session.bootstrap import SessionBootstrap


class _FakeSessionLike:
    """Minimal SessionLike for RecallCommandHandler tests."""

    def __init__(self, session_id: str, messages: list):
        self.session_id = session_id
        self._messages = messages

    def rebuild_view(self):
        from lanscoder.context.models import SessionView
        return SessionView(session_id=self.session_id, messages=list(self._messages))

    @property
    def runtime_state(self):
        from lanscoder.context.runtime_state import SessionRuntimeState
        return SessionRuntimeState()

    @property
    def current_turn(self):
        return max(
            (part.metadata.get("created_turn", 0) for msg in self._messages for part in msg.parts),
            default=0,
        )


def _make_msg(msg_id: str, role: str, content: str, turn: int = 1):
    from lanscoder.context.models import AgentMessage, MessagePart
    return AgentMessage(
        id=msg_id,
        session_id="sess_test",
        role=role,
        parts=[MessagePart(
            id=f"part_{msg_id}",
            message_id=msg_id,
            kind="text",
            content=content,
            metadata={"created_turn": turn, "turn_id": turn},
        )],
    )


class TestRecallCommandHandler:
    def test_handler_lists_user_messages(self):
        messages = [
            _make_msg("msg_01", "user", "hello world", 1),
            _make_msg("msg_02", "assistant", "hi there", 1),
            _make_msg("msg_03", "user", "do something please", 2),
            _make_msg("msg_04", "assistant", "ok", 2),
            _make_msg("msg_05", "user", "another thing", 3),
            _make_msg("msg_06", "assistant", "done", 3),
        ]
        session = _FakeSessionLike("sess_test", messages)
        handler = RecallCommandHandler(
            session=session,
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/recall")

        assert result.handled is True
        assert result.action is not None
        assert result.action["type"] == "recall_picker"
        turns = result.action["turns"]
        assert len(turns) == 3
        assert turns[0]["turn_number"] == 1
        assert turns[0]["message_id"] == "msg_01"
        assert "hello world" in turns[0]["summary"]
        assert turns[1]["turn_number"] == 2
        assert turns[1]["message_id"] == "msg_03"
        assert turns[2]["turn_number"] == 3
        assert turns[2]["message_id"] == "msg_05"

    def test_handler_ignores_non_recall_commands(self):
        handler = RecallCommandHandler(
            session=_FakeSessionLike("sess_test", []),
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/help")

        assert result.handled is False

    def test_handler_empty_session(self):
        handler = RecallCommandHandler(
            session=_FakeSessionLike("sess_test", []),
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/recall")

        assert result.handled is True
        assert "No messages" in result.output

    def test_handler_single_turn(self):
        messages = [
            _make_msg("msg_01", "user", "hello", 1),
            _make_msg("msg_02", "assistant", "hi", 1),
        ]
        session = _FakeSessionLike("sess_test", messages)
        handler = RecallCommandHandler(
            session=session,
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/recall")

        assert result.handled is True
        assert "Nothing to recall" in result.output
        assert result.action is None

    def test_recall_to_truncates_and_swaps(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_recall_test"
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

        # Build a real AgentSession so we can resume it
        bootstrap = SessionBootstrap(
            store=store,
            project_root=tmp_path,
            data_root=tmp_path,
        )
        session = bootstrap.create(session_id=sid)
        # Rebuild the session from the pre-written events
        session = bootstrap.resume(sid)

        swapped_session = None

        def on_recall(new_session):
            nonlocal swapped_session
            swapped_session = new_session

        handler = RecallCommandHandler(
            session=session,
            store=store,
            bootstrap=bootstrap,
            on_recall=on_recall,
        )

        output = handler.recall_to("msg_03")

        assert "Recalled" in output
        assert "turn 2" not in output  # removed
        assert swapped_session is not None
        assert swapped_session.session_id == sid

        # Verify the truncated session has correct messages
        view = swapped_session.rebuild_view()
        user_messages = [m for m in view.messages if m.role == "user"]
        assert len(user_messages) == 1
        assert user_messages[0].id == "msg_01"