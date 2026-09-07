from __future__ import annotations

from pathlib import Path

from lanscoder.context.runtime_replay import replay_runtime_state
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.providers.types import ChatResponse
from lanscoder.session.transcript import TranscriptBuilder


def _append_message(
    store: JsonlSessionStore,
    *,
    session_id: str,
    branch_id: str,
    message_id: str,
    role: str,
    content: str,
) -> None:
    store.append_journal_event(
        session_id=session_id,
        kind="message.appended",
        branch_id=branch_id,
        data={
            "message_id": message_id,
            "role": role,
            "parts": [{"id": f"part_{message_id}", "message_id": message_id, "kind": "text", "content": content, "metadata": {}}],
        },
    )


def test_context_consumers_replay_only_the_active_projection_with_interleaved_siblings(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_projection"
    root_writer = SessionEventWriter(store=store, session_id=session_id)
    root_writer.append_session_created(title="projection")
    root = root_writer.branch_context.branch_id  # type: ignore[union-attr]
    _append_message(store, session_id=session_id, branch_id=root, message_id="root_user", role="user", content="root")
    base_sequence = store.list_events(session_id)[-1].sequence
    store.append_journal_event(
        session_id=session_id,
        kind="session.recalled",
        branch_id="branch_a",
        data={"new_branch_id": "branch_a", "parent_branch_id": root, "base_sequence": base_sequence, "excluded_target_message_id": "old_a"},
    )
    _append_message(store, session_id=session_id, branch_id="branch_a", message_id="a_user", role="user", content="sibling a")
    store.append_journal_event(session_id=session_id, kind="provider.projection.consumed", branch_id="branch_a", data={"part_ids": ["a_part"]})
    store.append_journal_event(
        session_id=session_id,
        kind="session.recalled",
        branch_id="branch_b",
        data={"new_branch_id": "branch_b", "parent_branch_id": root, "base_sequence": base_sequence, "excluded_target_message_id": "old_b"},
    )
    _append_message(store, session_id=session_id, branch_id="branch_a", message_id="a_late", role="assistant", content="late sibling")
    _append_message(store, session_id=session_id, branch_id="branch_b", message_id="b_user", role="user", content="active b")
    store.append_journal_event(session_id=session_id, kind="provider.projection.consumed", branch_id="branch_b", data={"part_ids": ["b_part"]})
    store.append_journal_event(session_id=session_id, kind="observability.failed", data={"part_ids": ["global_part"]})

    view = store.rebuild_session_view(session_id)
    transcript = TranscriptBuilder(store).build(session_id)
    runtime = replay_runtime_state(store, session_id)

    assert [message.id for message in view.messages] == ["root_user", "b_user"]
    assert [entry.message_id for entry in transcript.entries] == ["root_user", "b_user"]
    assert runtime.consumed_tool_result_part_ids == {"b_part"}


def test_writer_continues_on_active_branch_after_restart(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_writer_branch"
    writer = SessionEventWriter(store=store, session_id=session_id)
    writer.append_session_created()
    root = writer.branch_context.branch_id  # type: ignore[union-attr]
    _append_message(store, session_id=session_id, branch_id=root, message_id="old", role="user", content="old")
    store.append_journal_event(
        session_id=session_id,
        kind="session.recalled",
        branch_id="new_branch",
        data={"new_branch_id": "new_branch", "parent_branch_id": root, "base_sequence": 1, "excluded_target_message_id": "old"},
    )

    resumed_writer = SessionEventWriter(store=store, session_id=session_id)
    resumed_writer.append_assistant_response(ChatResponse(provider="fake", model="fake", content="new"))

    appended = store.list_events(session_id)[-1]
    assert appended.branch_id == "new_branch"
    assert [message.parts[0].content for message in store.rebuild_session_view(session_id).messages] == ["new"]
