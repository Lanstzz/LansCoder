"""JSONL 会话存储:以追加日志持久化会话事件,并按事件序列重建会话视图。"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from lanscoder.context.checkpoint import Checkpoint
from lanscoder.context.events import SessionEvent
from lanscoder.context.metadata import merge_metadata_patch
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.planning.models import TaskPlan, TaskPlanError
from lanscoder.planning.validation import validate_plan
from lanscoder.journal.models import JournalEnvelope, new_branch_id
from lanscoder.journal.store import JournalStore

if TYPE_CHECKING:
    from lanscoder.session.branch import SessionBranchContext

EVENT_ROLE_MAP = {
    "message.appended": "user",
    "user_message": "user",
    "assistant_message": "assistant",
    "tool_result": "tool",
    "background_notification": "notification",
}

_MESSAGE_ROLES = {
    "user_message": "user",
    "assistant_message": "assistant",
    "tool_result": "tool",
    "background_notification": "notification",
}

_JOURNAL_KINDS = {
    "session_created": "session.created",
    "session_metadata_updated": "session.metadata_updated",
    **{event_type: "message.appended" for event_type in _MESSAGE_ROLES},
}


class SessionStoreCorruptError(ValueError):
    """会话存储损坏(事件或任务计划校验失败)。"""

    pass


def _journal_fields(event: SessionEvent) -> tuple[str, dict]:
    kind = _JOURNAL_KINDS.get(event.type, event.type.replace("_", "."))
    data = dict(event.payload)
    role = _MESSAGE_ROLES.get(event.type)
    if role is not None:
        data["role"] = role
    return kind, data


class JsonlSessionStore:
    """JSONL 会话存储:追加事件并从 active branch 重建会话视图。"""

    def __init__(self, root: str | Path) -> None:
        """初始化存储根目录与会话目录。"""
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.journal = JournalStore(self.root)

    def append_event(self, event: SessionEvent) -> JournalEnvelope:
        """Translate an in-process legacy event into the schema-v1 journal."""
        with self._lock:
            kind, data = _journal_fields(event)
            branch_id = self._branch_id_for_event(event.session_id, kind, data)
            envelope = self.journal.append(
                kind,
                data,
                session_id=event.session_id,
                event_id=event.id,
                occurred_at=event.created_at,
                branch_id=branch_id,
            )
            from lanscoder.session.index import SessionIndex

            SessionIndex(self.root).update_event(envelope)
            return envelope

    def append_journal_event(
        self,
        *,
        session_id: str,
        kind: str,
        data: dict,
        trace_id: str | None = None,
        observation_id: str | None = None,
        parent_observation_id: str | None = None,
        branch_id: str | None = None,
    ) -> JournalEnvelope:
        """Append one schema-v1 event and update its derived session index."""
        with self._lock:
            envelope = self.journal.append(
                kind,
                data,
                session_id=session_id,
                trace_id=trace_id,
                observation_id=observation_id,
                parent_observation_id=parent_observation_id,
                branch_id=branch_id,
            )
            from lanscoder.session.index import SessionIndex

            SessionIndex(self.root).update_event(envelope)
            return envelope

    def list_events(self, session_id: str) -> list[JournalEnvelope]:
        """Read schema-v1 journal envelopes for a session."""
        with self._lock:
            return self.journal.read_events(session_id)

    def _branch_id_for_event(self, session_id: str, kind: str, data: dict) -> str | None:
        if kind == "session.created":
            root_branch_id = str(data.get("root_branch_id") or new_branch_id())
            data["root_branch_id"] = root_branch_id
            return root_branch_id
        events = self.journal.read_events(session_id)
        if events:
            from lanscoder.session.branch import build_branch_topology

            topology = build_branch_topology(events)
            return topology.active_branch_id or topology.root_branch_id
        return "root"

    def rebuild_session_view(
        self,
        session_id: str,
        *,
        branch_context: SessionBranchContext | None = None,
    ) -> SessionView:
        """按事件序列重建会话视图。"""
        from lanscoder.session.branch import build_branch_topology
        from lanscoder.session.projection import active_projection, project_branch

        view = SessionView(session_id=session_id)
        events = self.list_events(session_id)
        if branch_context is None:
            projected = active_projection(events)
        else:
            if branch_context.session_id != session_id:
                raise ValueError("branch context session_id does not match session")
            if not any(
                event.kind == "session.created"
                and isinstance(event.data.get("root_branch_id"), str)
                and event.data["root_branch_id"]
                and isinstance(event.branch_id, str)
                and event.branch_id == event.data["root_branch_id"]
                for event in events
            ):
                raise ValueError("branch context requires a persisted session.created")
            topology = build_branch_topology(events)
            if branch_context.root_branch_id != topology.root_branch_id:
                raise ValueError("branch context root does not match the persisted topology")
            if branch_context.branch_id not in topology.branches:
                raise ValueError("branch context branch does not match the persisted topology")
            projected = project_branch(events, topology, branch_context.branch_id)
        for event in projected:
            self._apply_event(view, event, sequence=event.sequence)
        return view

    def original_user_message_texts(self, session_id: str) -> dict[str, str]:
        """返回各 user 消息的原始文本映射。"""
        from lanscoder.session.projection import active_projection

        texts: dict[str, str] = {}
        for event in active_projection(self.list_events(session_id)):
            if event.kind != "message.appended" or event.data.get("role") != "user":
                continue
            message_id = str(event.data.get("message_id") or "")
            if not message_id:
                continue
            texts[message_id] = "\n".join(str(part.get("content") or "") for part in event.data.get("parts") or [] if isinstance(part, dict) and part.get("kind") == "text" and part.get("content"))
        return texts

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def truncate_before_message(self, session_id: str, message_id: str) -> int:
        """把会话截断到某条 user 消息之前(用于回退到历史点)。"""
        with self._lock:
            path = self._session_path(session_id)
            if not path.exists():
                raise FileNotFoundError(f"Session file not found: {path}")

            events = self.list_events(session_id)
            if not events:
                raise ValueError(f"Session {session_id} has no events")

            target_line: int | None = None
            for index, event in enumerate(events):
                if event.type == "user_message" and str(event.payload.get("message_id") or "") == message_id:
                    target_line = index
                    break

            if target_line is None:
                for index, event in enumerate(events):
                    if str(event.payload.get("message_id") or "") == message_id:
                        raise ValueError(f"message_id {message_id} is not a user_message event (type={event.type}); can only recall to user message boundaries")
                raise ValueError(f"message_id not found: {message_id} in session {session_id}")

            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

            if target_line == 0:
                raise ValueError("Cannot truncate before the session_created event")

            retained_lines = lines[:target_line]

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
        """删除会话文件、归档目录并重建索引。"""
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

    def _apply_event(self, view: SessionView, event: SessionEvent | JournalEnvelope, *, sequence: int) -> None:
        """Apply one persistent envelope or one in-memory legacy event."""
        if isinstance(event, JournalEnvelope):
            self._apply_journal_envelope(view, event, sequence=sequence)
            return

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

    @staticmethod
    def _apply_journal_envelope(view: SessionView, event: JournalEnvelope, *, sequence: int) -> None:
        kind = event.kind
        data = event.data
        if kind in {"session.created", "session.metadata_updated"}:
            view.metadata = merge_metadata_patch(view.metadata, data)
            view.metadata["session_id"] = event.session_id
            return

        if kind == "checkpoint.created":
            view.checkpoints.append(
                Checkpoint.from_dict(
                    _checkpoint_payload_from_data(
                        data,
                        session_id=event.session_id,
                        occurred_at=event.occurred_at,
                        sequence=sequence,
                    )
                )
            )
            return

        if kind == "compaction.completed":
            _apply_compaction_replacements_from_data(view, data)
            return

        if kind == "message.part.metadata.updated":
            _apply_message_part_metadata_update_from_data(view, data)
            return

        if kind == "task.plan.updated":
            _apply_task_plan_payload_from_data(view, data, event_id=event.event_id)
            return

        if kind != "message.appended":
            return
        role = str(data.get("role") or "")
        if role not in {"user", "assistant", "tool", "notification"}:
            return
        view.messages.append(
            _message_from_payload(
                data,
                session_id=event.session_id,
                occurred_at=event.occurred_at,
                role=role,
            )
        )


def _message_from_event(event: SessionEvent, *, role: str) -> AgentMessage:
    """从事件构造 AgentMessage。"""
    return _message_from_payload(
        event.payload,
        session_id=event.session_id,
        occurred_at=event.created_at,
        role=role,
    )


def _message_from_payload(
    payload: dict,
    *,
    session_id: str,
    occurred_at: str,
    role: str,
) -> AgentMessage:
    message_id = str(payload["message_id"])
    parts = _parts_from_payload(payload.get("parts", []), message_id=message_id)
    return AgentMessage(
        id=message_id,
        session_id=session_id,
        role=role,
        parts=parts,
        created_at=occurred_at,
        metadata=dict(payload.get("metadata") or {}),
    )


def _parts_from_payload(parts: Iterable[dict[str, object]], *, message_id: str) -> list[MessagePart]:
    """从事件载荷构造消息部件列表。"""
    result: list[MessagePart] = []
    for part in parts:
        data = dict(part)
        data.setdefault("message_id", message_id)
        result.append(MessagePart.from_dict(data))
    return result


def _checkpoint_payload(event: SessionEvent, *, sequence: int) -> dict[str, object]:
    """补齐检查点载荷的缺省字段。"""
    return _checkpoint_payload_from_data(
        event.payload,
        session_id=event.session_id,
        occurred_at=event.created_at,
        sequence=sequence,
    )


def _checkpoint_payload_from_data(
    data: dict,
    *,
    session_id: str,
    occurred_at: str,
    sequence: int,
) -> dict[str, object]:
    payload: dict[str, object] = dict(data)
    payload.setdefault("created_at", occurred_at)
    payload.setdefault("session_id", session_id)
    payload.setdefault("sequence", sequence)
    return payload


def _apply_compaction_replacements(view: SessionView, event: SessionEvent) -> None:
    """把压缩事件的部件替换应用到视图。"""
    _apply_compaction_replacements_from_data(view, event.payload)


def _apply_compaction_replacements_from_data(view: SessionView, data: dict) -> None:
    event_payload = data.get("event")
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
    """把消息部件的元数据更新应用到视图。"""
    _apply_message_part_metadata_update_from_data(view, event.payload)


def _apply_message_part_metadata_update_from_data(view: SessionView, data: dict) -> None:
    message_id = str(data.get("message_id") or "")
    part_id = str(data.get("part_id") or "")
    metadata = data.get("metadata")
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
    """校验并应用任务计划更新事件。"""
    _apply_task_plan_payload_from_data(view, event.payload, event_id=event.id)


def _apply_task_plan_payload_from_data(view: SessionView, data: dict, *, event_id: str) -> None:
    try:
        plan = TaskPlan.from_dict(data.get("snapshot"))  # type: ignore[arg-type]
        validate_plan(plan)
    except (TaskPlanError, TypeError) as error:
        raise SessionStoreCorruptError(f"invalid task_plan_updated snapshot in event {event_id}: {error}") from error

    previous_revision = data.get("previous_revision")
    revision = data.get("revision")
    if isinstance(previous_revision, bool) or not isinstance(previous_revision, int) or isinstance(revision, bool) or not isinstance(revision, int):
        raise SessionStoreCorruptError(f"task_plan_updated revision chain is invalid in event {event_id}")
    expected_previous = view.task_plan.revision if view.task_plan is not None else 0
    if previous_revision != expected_previous or revision != previous_revision + 1:
        raise SessionStoreCorruptError(f"task_plan_updated revision chain is invalid in event {event_id}: expected previous {expected_previous}, got {previous_revision} -> {revision}")
    if revision != plan.revision:
        raise SessionStoreCorruptError(f"task_plan_updated revision mismatch in event {event_id}")
    view.task_plan = plan


class InMemorySessionStore(JsonlSessionStore):
    """内存会话存储:不建目录、不写盘,复用基类 ``_apply_event`` 重建逻辑。

    用于 L1 ``agent_loop``(session-free):会话生命周期随循环结束而结束。
    ``root`` 指向一个不会被创建的哨兵路径,满足 ``AgentSession`` 对
    ``store.root`` 的既有引用;任何路径都不落盘。
    """

    def __init__(self) -> None:
        # 不调用 super().__init__,避免创建 sessions 目录。
        self.root = Path(tempfile.gettempdir()) / f"lanscoder-inmemory-{os.getpid()}-{id(self)}"
        self._lock = threading.RLock()
        self._events: dict[str, list[SessionEvent]] = {}

    def append_event(self, event: SessionEvent) -> None:
        with self._lock:
            self._events.setdefault(event.session_id, []).append(event)

    def list_events(self, session_id: str) -> list[SessionEvent]:
        with self._lock:
            return list(self._events.get(session_id, ()))

    def rebuild_session_view(
        self,
        session_id: str,
        *,
        branch_context: SessionBranchContext | None = None,
    ) -> SessionView:
        if branch_context is not None:
            if branch_context.session_id != session_id:
                raise ValueError("branch context session_id does not match session")
            raise ValueError("branch projection requires a journal-backed session")
        view = SessionView(session_id=session_id)
        for sequence, event in enumerate(self.list_events(session_id), start=1):
            self._apply_event(view, event, sequence=sequence)
        return view

    def original_user_message_texts(self, session_id: str) -> dict[str, str]:
        texts: dict[str, str] = {}
        for event in self.list_events(session_id):
            if event.type != "user_message":
                continue
            message_id = str(event.payload.get("message_id") or "")
            if not message_id:
                continue
            texts[message_id] = "\n".join(str(part.get("content") or "") for part in event.payload.get("parts") or [] if isinstance(part, dict) and part.get("kind") == "text" and part.get("content"))
        return texts

    def truncate_before_message(self, session_id: str, message_id: str) -> int:
        with self._lock:
            events = self.list_events(session_id)
            if not events:
                raise ValueError(f"Session {session_id} has no events")

            target_index: int | None = None
            for index, event in enumerate(events):
                if event.type == "user_message" and str(event.payload.get("message_id") or "") == message_id:
                    target_index = index
                    break

            if target_index is None:
                for index, event in enumerate(events):
                    if str(event.payload.get("message_id") or "") == message_id:
                        raise ValueError(f"message_id {message_id} is not a user_message event (type={events[index].type}); can only recall to user message boundaries")
                raise ValueError(f"message_id not found: {message_id} in session {session_id}")

            if target_index == 0:
                raise ValueError("Cannot truncate before the session_created event")

            retained = events[:target_index]
            self._events[session_id] = retained
            return len(retained)

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            return self._events.pop(session_id, None) is not None
