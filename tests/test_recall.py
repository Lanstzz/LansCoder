"""Behavior tests for append-only branch recall."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from lanscoder.app.recall_commands import RecallCommandHandler
from lanscoder.context.compaction import CompactionEvent
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.types import ChatResponse
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.index import SessionIndex
from lanscoder.session.resume import ResumeService
from lanscoder.storage import LansCoderPaths


@dataclass
class _BackgroundManager:
    abandon_calls: list[tuple[str, int]]

    def abandon_since(self, session_id: str, *, min_dispatch_turn: int) -> None:
        self.abandon_calls.append((session_id, min_dispatch_turn))


def _bootstrap(tmp_path: Path) -> tuple[JsonlSessionStore, SessionBootstrap]:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    return store, SessionBootstrap(store=store, project_root=tmp_path, paths=paths)


def _handler(session, store: JsonlSessionStore, bootstrap: SessionBootstrap, swapped: list) -> RecallCommandHandler:
    return RecallCommandHandler(
        session=session,
        store=store,
        bootstrap=bootstrap,
        on_recall=swapped.append,
    )


def test_recall_appends_branch_switch_and_returns_selected_input(tmp_path: Path) -> None:
    store, bootstrap = _bootstrap(tmp_path)
    session = bootstrap.create(session_id="sess_recall")
    first = session.append_user_message("first request")
    session.append_assistant_response(ChatResponse(provider="fake", model="fake", content="first answer"))
    target = session.append_user_message("rewrite this request")
    session.append_assistant_response(ChatResponse(provider="fake", model="fake", content="second answer"))
    before = store.list_events(session.session_id)
    swapped: list = []

    result = _handler(session, store, bootstrap, swapped).handle(f"/recall {target}")

    events = store.list_events(session.session_id)
    recalled = events[-1]
    target_index = next(index for index, event in enumerate(before) if event.data.get("message_id") == target)
    assert result.action == {"type": "replay_session", "recalled_text": "rewrite this request"}
    assert len(events) == len(before) + 1
    assert [event.event_id for event in events[:-1]] == [event.event_id for event in before]
    assert recalled.kind == "session.recalled"
    assert recalled.branch_id == recalled.data["new_branch_id"]
    assert recalled.data == {
        "new_branch_id": recalled.branch_id,
        "parent_branch_id": before[-1].branch_id,
        "base_sequence": before[target_index - 1].sequence,
        "excluded_target_message_id": target,
    }
    assert swapped[0].writer.branch_context is not None
    assert swapped[0].writer.branch_context.branch_id == recalled.branch_id
    assert [message.id for message in swapped[0].rebuild_view().messages if message.role == "user"] == [first]


def test_recall_restart_and_second_recall_follow_current_active_path(tmp_path: Path) -> None:
    store, bootstrap = _bootstrap(tmp_path)
    session = bootstrap.create(session_id="sess_repeated_recall")
    first = session.append_user_message("first")
    session.append_assistant_response(ChatResponse(provider="fake", model="fake", content="answer"))
    second = session.append_user_message("second")
    session.append_assistant_response(ChatResponse(provider="fake", model="fake", content="answer"))
    first_swap: list = []
    _handler(session, store, bootstrap, first_swap).recall_to(second)
    recalled_once = first_swap[0]
    branch_message = recalled_once.append_user_message("replacement")
    restarted = bootstrap.resume(session.session_id)
    assert [message.id for message in restarted.rebuild_view().messages if message.role == "user"] == [first, branch_message]

    second_swap: list = []
    _handler(restarted, store, bootstrap, second_swap).recall_to(branch_message)

    recalls = [event for event in store.list_events(session.session_id) if event.kind == "session.recalled"]
    assert len(recalls) == 2
    assert recalls[1].data["parent_branch_id"] == recalls[0].data["new_branch_id"]
    assert recalls[1].data["excluded_target_message_id"] == branch_message
    assert [message.id for message in second_swap[0].rebuild_view().messages if message.role == "user"] == [first]
    assert any(event.data.get("message_id") == second for event in store.list_events(session.session_id))


def test_recall_never_abandons_background_work(tmp_path: Path) -> None:
    store, bootstrap = _bootstrap(tmp_path)
    session = bootstrap.create(session_id="sess_background")
    target = session.append_user_message("keep background running")
    manager = _BackgroundManager(abandon_calls=[])
    swapped: list = []
    handler = _handler(session, store, bootstrap, swapped)
    handler.background_manager = manager  # type: ignore[assignment]

    handler.recall_to(target)

    assert manager.abandon_calls == []


def test_recall_picker_only_lists_active_branch_checkpoints(tmp_path: Path) -> None:
    store, bootstrap = _bootstrap(tmp_path)
    session = bootstrap.create(session_id="sess_active_picker")
    first = session.append_user_message("first")
    hidden = session.append_user_message("hidden after recall")
    swapped: list = []
    _handler(session, store, bootstrap, swapped).recall_to(hidden)
    visible = swapped[0].append_user_message("active replacement")

    result = _handler(swapped[0], store, bootstrap, []).handle("/recall")

    assert [turn["message_id"] for turn in result.action["turns"]] == [first, visible]


def _create_session(tmp_path: Path, session_id: str):
    store, bootstrap = _bootstrap(tmp_path)
    return store, bootstrap, bootstrap.create(session_id=session_id)


def _append_turn(session, user_text: str, assistant_text: str) -> str:
    message_id = session.append_user_message(user_text)
    session.append_assistant_response(ChatResponse(provider="fake", model="fake", content=assistant_text))
    return message_id


class TestTruncateBeforeMessage:
    def test_truncate_to_specific_message(self, tmp_path: Path) -> None:
        store, _, session = _create_session(tmp_path, "sess_truncate_specific")
        first = _append_turn(session, "hello", "hi there")
        target = _append_turn(session, "do something", "ok done")

        retained = store.truncate_before_message(session.session_id, target)

        remaining = store.list_events(session.session_id)
        assert retained == len(remaining)
        assert [event.data.get("message_id") for event in remaining if event.kind == "message.appended"] == [first, remaining[-1].data["message_id"]]
        assert remaining[-1].data["role"] == "assistant"

    def test_truncate_to_first_turn_keeps_session_metadata(self, tmp_path: Path) -> None:
        store, bootstrap, session = _create_session(tmp_path, "sess_truncate_first")
        target = _append_turn(session, "hello", "hi there")

        store.truncate_before_message(session.session_id, target)

        remaining = store.list_events(session.session_id)
        assert [event.kind for event in remaining] == ["session.created"]
        created = remaining[0]
        assert created.data["session_id"] == session.session_id
        assert created.data["project_id"] == bootstrap.paths.project_id
        assert created.data["project_root"] == str(bootstrap.paths.project_root)
        assert created.data["kind"] == "primary"
        assert not any(event.kind == "message.appended" for event in remaining)

    def test_truncate_preserves_session_created_and_prior_assistant_message(self, tmp_path: Path) -> None:
        store, _, session = _create_session(tmp_path, "sess_truncate_preserve")
        first = _append_turn(session, "first", "first answer")
        target = _append_turn(session, "second", "second answer")

        store.truncate_before_message(session.session_id, target)

        remaining = store.list_events(session.session_id)
        assert remaining[0].kind == "session.created"
        assert remaining[0].data["session_id"] == session.session_id
        assert [event.data.get("message_id") for event in remaining if event.kind == "message.appended"] == [first, remaining[-1].data["message_id"]]
        assert remaining[-1].data["role"] == "assistant"

    def test_truncate_is_atomic_for_missing_message(self, tmp_path: Path) -> None:
        store, _, session = _create_session(tmp_path, "sess_truncate_atomic")
        _append_turn(session, "hello", "hi")
        before = store._session_path(session.session_id).read_text(encoding="utf-8")

        with pytest.raises(ValueError, match="message_id not found"):
            store.truncate_before_message(session.session_id, "missing")

        assert store._session_path(session.session_id).read_text(encoding="utf-8") == before

    def test_truncate_rejects_non_user_message(self, tmp_path: Path) -> None:
        store, _, session = _create_session(tmp_path, "sess_truncate_role")
        _append_turn(session, "hello", "hi")
        assistant_message = session.rebuild_view().messages[-1].id

        with pytest.raises(ValueError, match="is not a user_message"):
            store.truncate_before_message(session.session_id, assistant_message)

    def test_truncate_rejects_missing_session(self, tmp_path: Path) -> None:
        store, _ = _bootstrap(tmp_path)

        with pytest.raises(FileNotFoundError):
            store.truncate_before_message("sess_missing", "message")


def _message(message_id: str, role: str, content: str, turn: int = 1) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_fake",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="text",
                content=content,
                metadata={"created_turn": turn, "turn_id": turn},
            )
        ],
    )


@dataclass
class _FakeSession:
    session_id: str
    messages: list[AgentMessage]

    @property
    def current_turn(self) -> int:
        return max((part.metadata["created_turn"] for message in self.messages for part in message.parts), default=0)

    def rebuild_view(self) -> SessionView:
        return SessionView(session_id=self.session_id, messages=list(self.messages))


def _fake_handler(messages: list[AgentMessage], *, busy_check=lambda: False) -> RecallCommandHandler:
    return RecallCommandHandler(
        session=_FakeSession("sess_fake", messages),
        store=None,  # type: ignore[arg-type]
        bootstrap=None,  # type: ignore[arg-type]
        on_recall=lambda _: None,
        busy_check=busy_check,
    )


class TestRecallCommandHandling:
    def test_handler_lists_only_user_messages(self) -> None:
        handler = _fake_handler(
            [
                _message("user_1", "user", "first", 1),
                _message("assistant_1", "assistant", "answer", 1),
                _message("user_2", "user", "second", 2),
            ]
        )

        result = handler.handle("/recall")

        assert result.action == {
            "type": "recall_picker",
            "turns": [
                {"turn_number": 1, "message_id": "user_1", "summary": "first"},
                {"turn_number": 2, "message_id": "user_2", "summary": "second"},
            ],
        }

    def test_handler_excludes_background_notifications(self) -> None:
        notification = _message("notification", "notification", "background", 1)
        notification.parts[0].metadata["background_job_id"] = "job_1"
        handler = _fake_handler([_message("user_1", "user", "first"), notification, _message("user_2", "user", "second", 2)])

        result = handler.handle("/recall")

        assert [turn["message_id"] for turn in result.action["turns"]] == ["user_1", "user_2"]

    def test_handler_ignores_non_recall_commands(self) -> None:
        assert _fake_handler([]).handle("/help").handled is False

    def test_handler_reports_empty_session(self) -> None:
        result = _fake_handler([]).handle("/recall")
        assert result.handled is True
        assert result.output == "No messages to recall"

    def test_handler_lists_a_single_turn(self) -> None:
        result = _fake_handler([_message("user_1", "user", "first")]).handle("/recall")

        assert result.action["turns"] == [{"turn_number": 1, "message_id": "user_1", "summary": "first"}]

    def test_handler_rejects_recall_while_busy(self) -> None:
        handler = _fake_handler([_message("user_1", "user", "first")], busy_check=lambda: True)

        for command in ("/recall", "/recall user_1"):
            result = handler.handle(command)
            assert result.handled is True
            assert "尚未结束" in result.output

    def test_handler_allows_recall_when_not_busy(self) -> None:
        result = _fake_handler([_message("user_1", "user", "first")], busy_check=lambda: False).handle("/recall")

        assert result.action["type"] == "recall_picker"


class TestRecallCompactionAndResume:
    def test_picker_uses_original_text_after_compaction(self, tmp_path: Path) -> None:
        store, bootstrap, session = _create_session(tmp_path, "sess_compacted_picker")
        first = _append_turn(session, "first request", "first answer")
        _append_turn(session, "second request", "second answer")
        first_part = session.rebuild_view().messages[0].parts[0]
        session.writer.append_compaction_completed(
            trigger="manual",
            target_tokens=1,
            event=CompactionEvent(
                input_fingerprint="compacted",
                before_tokens=2,
                after_tokens=1,
                levels_attempted=["l1"],
                stopped_at="l1",
                changed_parts=1,
                replacements=[
                    {
                        "message_id": first,
                        "source_part_id": first_part.id,
                        "replacement_part": {
                            "id": first_part.id,
                            "message_id": first,
                            "kind": "text",
                            "content": "",
                            "metadata": dict(first_part.metadata),
                        },
                    }
                ],
            ),
        )

        result = _handler(session, store, bootstrap, []).handle("/recall")

        assert [turn["summary"] for turn in result.action["turns"]] == ["first request", "second request"]

    def test_recall_to_compacted_message_returns_original_input(self, tmp_path: Path) -> None:
        store, bootstrap, session = _create_session(tmp_path, "sess_compacted_recall")
        message_id = session.append_user_message("recover this text")
        part = session.rebuild_view().messages[0].parts[0]
        session.writer.append_compaction_completed(
            trigger="manual",
            target_tokens=1,
            event=CompactionEvent(
                input_fingerprint="compacted",
                before_tokens=2,
                after_tokens=1,
                levels_attempted=["l1"],
                stopped_at="l1",
                changed_parts=1,
                replacements=[
                    {
                        "message_id": message_id,
                        "source_part_id": part.id,
                        "replacement_part": {"id": part.id, "message_id": message_id, "kind": "text", "content": "", "metadata": dict(part.metadata)},
                    }
                ],
            ),
        )

        result = _handler(session, store, bootstrap, []).handle(f"/recall {message_id}")

        assert result.action == {"type": "replay_session", "recalled_text": "recover this text"}

    def test_recall_uses_resume_service(self, tmp_path: Path) -> None:
        store, bootstrap, session = _create_session(tmp_path, "sess_resume_service")
        first = _append_turn(session, "first", "answer")
        target = _append_turn(session, "second", "answer")
        swapped: list = []
        handler = _handler(session, store, bootstrap, swapped)
        handler.resume_service = ResumeService(
            store=store,
            project_root=tmp_path,
            paths=LansCoderPaths(storage_root=store.root, project_root=tmp_path),
            catalog=SessionCatalog(store.root),
        )

        handler.recall_to(target)

        assert [message.id for message in swapped[0].rebuild_view().messages if message.role == "user"] == [first]


def test_truncate_rebuilds_index_for_its_standalone_api(tmp_path: Path) -> None:
    store, _, session = _create_session(tmp_path, "sess_truncate_index")
    _append_turn(session, "first", "answer")
    target = _append_turn(session, "second", "answer")
    index = SessionIndex(store.root)
    assert index.list_records()[0].user_turn_count == 2

    store.truncate_before_message(session.session_id, target)
    index.rebuild_session(session.session_id)

    assert index.list_records()[0].user_turn_count == 1


def test_recall_of_the_only_message_keeps_history_and_empties_active_view(tmp_path: Path) -> None:
    store, bootstrap, session = _create_session(tmp_path, "sess_single_recall")
    target = _append_turn(session, "only request", "only answer")
    before = store.list_events(session.session_id)
    swapped: list = []

    _handler(session, store, bootstrap, swapped).recall_to(target)

    assert len(store.list_events(session.session_id)) == len(before) + 1
    assert store.list_events(session.session_id)[-1].kind == "session.recalled"
    assert swapped[0].rebuild_view().messages == []


def test_recall_then_continue_writes_only_the_new_branch_to_active_view(tmp_path: Path) -> None:
    store, bootstrap, session = _create_session(tmp_path, "sess_recall_continue")
    first = _append_turn(session, "first", "first answer")
    target = _append_turn(session, "second", "second answer")
    swapped: list = []
    _handler(session, store, bootstrap, swapped).recall_to(target)
    recalled = swapped[0]
    replacement = recalled.append_user_message("replacement")
    recalled.append_assistant_response(ChatResponse(provider="fake", model="fake", content="replacement answer"))

    view = recalled.rebuild_view()

    assert [message.id for message in view.messages if message.role == "user"] == [first, replacement]
    assert any(event.data.get("message_id") == target for event in store.list_events(session.session_id))


def test_recall_keeps_the_session_index_usable_without_truncating_history(tmp_path: Path) -> None:
    store, bootstrap, session = _create_session(tmp_path, "sess_recall_index")
    _append_turn(session, "first", "answer")
    target = _append_turn(session, "second", "answer")
    before = store.list_events(session.session_id)

    _handler(session, store, bootstrap, []).recall_to(target)

    assert len(store.list_events(session.session_id)) == len(before) + 1
    assert SessionIndex(store.root).list_records()[0].session_id == session.session_id
