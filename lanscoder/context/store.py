"""基于 JSONL 的会话事件存储。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Iterable

from lanscoder.context.checkpoint import Checkpoint
from lanscoder.context.events import SessionEvent
from lanscoder.context.metadata import merge_metadata_patch
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.planning.models import TaskPlan, TaskPlanError
from lanscoder.planning.validation import validate_plan

EVENT_ROLE_MAP = {
    "user_message": "user",
    "assistant_message": "assistant",
    "tool_result": "tool",
    "background_notification": "notification",
}


class SessionStoreCorruptError(ValueError):
    """A persisted event cannot be replayed into a trustworthy session view."""


class JsonlSessionStore:
    """append-only JSONL store。

    当前阶段选择 JSONL 是为了让 resume、压缩事件和调试记录都能被人工阅读。后续迁移
    SQLite 时，外部仍应保留 `append_event/list_events/rebuild_session_view` 这组边界。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        # Guards the session JSONL files against concurrent read/write/truncate
        # from different threads (e.g. the TUI event-loop thread running /recall
        # while a worker thread appends an agent event). Reentrant so that
        # truncate_before_message can call list_events while already holding it.
        self._lock = threading.RLock()

    def append_event(self, event: SessionEvent) -> None:
        with self._lock:
            path = self._session_path(event.session_id)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
                file.write("\n")
            from lanscoder.session.index import SessionIndex

            SessionIndex(self.root).update_event(event)

    def list_events(self, session_id: str) -> list[SessionEvent]:
        with self._lock:
            path = self._session_path(session_id)
            if not path.exists():
                return []

            events: list[SessionEvent] = []
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    if line.strip():
                        events.append(SessionEvent.from_dict(json.loads(line)))
            return events

    def rebuild_session_view(self, session_id: str) -> SessionView:
        view = SessionView(session_id=session_id)
        for sequence, event in enumerate(self.list_events(session_id), start=1):
            self._apply_event(view, event, sequence=sequence)
        return view

    def original_user_message_texts(self, session_id: str) -> dict[str, str]:
        """Return original text per user_message event, untouched by compaction.

        压缩只追加 compaction_completed 替换事件、从不改写原始 user_message
        事件，所以这里总能读到当初真正的话。transcript 已按同样原则从
        list_events 派生内容。
        """

        texts: dict[str, str] = {}
        for event in self.list_events(session_id):
            if event.type != "user_message":
                continue
            message_id = str(event.payload.get("message_id") or "")
            if not message_id:
                continue
            texts[message_id] = "\n".join(
                str(part.get("content") or "")
                for part in event.payload.get("parts") or []
                if isinstance(part, dict) and part.get("kind") == "text" and part.get("content")
            )
        return texts

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def truncate_before_message(self, session_id: str, message_id: str) -> int:
        """Truncate the session JSONL to exclude the given message and everything after it.

        The target message_id must belong to a user_message event. The file is
        truncated to the line immediately before that event. The session_created
        event (first line) is always preserved.

        Returns:
            The number of lines retained after truncation.

        Raises:
            FileNotFoundError: if the session file does not exist.
            ValueError: if message_id is not found or does not belong to a user_message.
        """
        with self._lock:
            path = self._session_path(session_id)
            if not path.exists():
                raise FileNotFoundError(f"Session file not found: {path}")

            events = self.list_events(session_id)
            if not events:
                raise ValueError(f"Session {session_id} has no events")

            # Find the target line index (0-based)
            target_line: int | None = None
            for index, event in enumerate(events):
                if event.type == "user_message" and str(event.payload.get("message_id") or "") == message_id:
                    target_line = index
                    break

            if target_line is None:
                # Check if the message_id exists at all (but with wrong type)
                for index, event in enumerate(events):
                    if str(event.payload.get("message_id") or "") == message_id:
                        raise ValueError(
                            f"message_id {message_id} is not a user_message event (type={event.type}); "
                            f"can only recall to user message boundaries"
                        )
                raise ValueError(f"message_id not found: {message_id} in session {session_id}")

            # Read all lines from the file
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

            # Validate: first line must be session_created
            if target_line == 0:
                raise ValueError("Cannot truncate before the session_created event")

            retained_lines = lines[:target_line]

            # Atomic write via temp file
            tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{session_id}.", suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.writelines(retained_lines)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            return len(retained_lines)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session's persisted artifacts (JSONL, index entry, archives).

        Idempotent and best-effort: deleting an unknown session returns ``False``
        without raising. Child (subagent) sessions call this after finishing so
        their ephemeral transcripts never surface in ``/resume``.
        """
        with self._lock:
            path = self._session_path(session_id)
            if not path.exists():
                return False
            path.unlink()

            archive_dir = self.root / "archives" / session_id
            if archive_dir.exists():
                shutil.rmtree(archive_dir, ignore_errors=True)

            from lanscoder.session.index import SessionIndex

            SessionIndex(self.root).rebuild_session(session_id)
            return True

    def _apply_event(self, view: SessionView, event: SessionEvent, *, sequence: int) -> None:
        if event.type in {"session_created", "session_metadata_updated"}:
            view.metadata = merge_metadata_patch(view.metadata, event.payload)
            view.metadata["session_id"] = event.session_id
            return

        if event.type == "checkpoint_created":
            view.checkpoints.append(Checkpoint.from_dict(_checkpoint_payload(event, sequence=sequence)))
            return

        if event.type == "compaction_completed":
            _apply_compaction_replacements(view, event)
            return

        if event.type == "message_part_metadata_updated":
            _apply_message_part_metadata_update(view, event)
            return

        if event.type == "task_plan_updated":
            _apply_task_plan_payload(view, event)
            return

        role = EVENT_ROLE_MAP.get(event.type)
        if role is None:
            return

        message = _message_from_event(event, role=role)
        view.messages.append(message)


def _message_from_event(event: SessionEvent, *, role: str) -> AgentMessage:
    payload = event.payload
    message_id = str(payload["message_id"])
    parts = _parts_from_payload(payload.get("parts", []), message_id=message_id)
    return AgentMessage(
        id=message_id,
        session_id=event.session_id,
        role=role,
        parts=parts,
        created_at=event.created_at,
        metadata=dict(payload.get("metadata") or {}),
    )


def _parts_from_payload(parts: Iterable[dict[str, object]], *, message_id: str) -> list[MessagePart]:
    result: list[MessagePart] = []
    for part in parts:
        data = dict(part)
        data.setdefault("message_id", message_id)
        result.append(MessagePart.from_dict(data))
    return result


def _checkpoint_payload(event: SessionEvent, *, sequence: int) -> dict[str, object]:
    payload: dict[str, object] = dict(event.payload)
    payload.setdefault("created_at", event.created_at)
    payload.setdefault("session_id", event.session_id)
    payload.setdefault("sequence", sequence)
    return payload


def _apply_compaction_replacements(view: SessionView, event: SessionEvent) -> None:
    event_payload = event.payload.get("event")
    if not isinstance(event_payload, dict):
        return

    replacements = event_payload.get("replacements")
    if not isinstance(replacements, list):
        return

    part_index: dict[tuple[str, str], tuple[AgentMessage, int]] = {}
    for message in view.messages:
        for index, part in enumerate(message.parts):
            part_index[(message.id, part.id)] = (message, index)

    for item in replacements:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("message_id") or "")
        source_part_id = str(item.get("source_part_id") or "")
        replacement_part = item.get("replacement_part")
        if not message_id or not source_part_id or not isinstance(replacement_part, dict):
            continue
        target = part_index.get((message_id, source_part_id))
        if target is None:
            continue
        message, index = target
        replacement_data = dict(replacement_part)
        replacement_data.setdefault("message_id", message_id)
        message.parts[index] = MessagePart.from_dict(replacement_data)


def _apply_message_part_metadata_update(view: SessionView, event: SessionEvent) -> None:
    message_id = str(event.payload.get("message_id") or "")
    part_id = str(event.payload.get("part_id") or "")
    metadata = event.payload.get("metadata")
    if not message_id or not part_id or not isinstance(metadata, dict):
        return
    for message in view.messages:
        if message.id != message_id:
            continue
        for part in message.parts:
            if part.id == part_id:
                part.metadata.update(metadata)
                return


def _apply_task_plan_payload(view: SessionView, event: SessionEvent) -> None:
    try:
        plan = TaskPlan.from_dict(event.payload.get("snapshot"))  # type: ignore[arg-type]
        validate_plan(plan)
    except (TaskPlanError, TypeError) as error:
        raise SessionStoreCorruptError(f"invalid task_plan_updated snapshot in event {event.id}: {error}") from error

    previous_revision = event.payload.get("previous_revision")
    revision = event.payload.get("revision")
    if isinstance(previous_revision, bool) or not isinstance(previous_revision, int) or isinstance(revision, bool) or not isinstance(revision, int):
        raise SessionStoreCorruptError(f"task_plan_updated revision chain is invalid in event {event.id}")
    expected_previous = view.task_plan.revision if view.task_plan is not None else 0
    if previous_revision != expected_previous or revision != previous_revision + 1:
        raise SessionStoreCorruptError(f"task_plan_updated revision chain is invalid in event {event.id}: " f"expected previous {expected_previous}, got {previous_revision} -> {revision}")
    if revision != plan.revision:
        raise SessionStoreCorruptError(f"task_plan_updated revision mismatch in event {event.id}")
    view.task_plan = plan
