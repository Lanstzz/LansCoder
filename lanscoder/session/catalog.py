from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lanscoder.context.metadata import merge_metadata_patch
from lanscoder.journal.models import JournalEnvelope
from lanscoder.journal.store import JournalStore
from lanscoder.session.errors import (
    SessionCorruptError,
    SessionEmptyError,
    SessionInvalidIdError,
    SessionNotFoundError,
)
from lanscoder.session.models import SessionRecord
from lanscoder.storage.paths import LansCoderPaths
from lanscoder.utils.text import optional_str

MESSAGE_EVENT_TYPES = {"message.appended"}
PREVIEW_CHARS = 80
SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def require_usable_record(record: SessionRecord) -> SessionRecord:
    if record.status == "corrupt":
        raise SessionCorruptError(record.error or f"session is corrupt: {record.session_id}")
    if record.status == "empty":
        raise SessionEmptyError(f"session is empty: {record.session_id}")
    return record


class SessionCatalog:

    def __init__(self, root: str | Path, *, project_id: str | None = None, project_root: str | Path | None = None) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        if project_id is not None and project_root is not None:
            raise ValueError("provide project_id or project_root, not both")
        self.project_id = project_id
        if project_root is not None:
            self.project_id = LansCoderPaths(storage_root=self.root, project_root=project_root).project_id

    def list_sessions(self) -> list[SessionRecord]:
        if not self.sessions_dir.exists():
            return []

        from lanscoder.session.index import SessionIndex

        return SessionIndex(self.root, project_id=self.project_id).list_records()

    def get_session(self, session_id: str) -> SessionRecord:
        _validate_session_id(session_id)
        path = self.sessions_dir / f"{session_id}.jsonl"
        if not path.exists():
            raise SessionNotFoundError(f"session not found: {session_id}")
        return record_from_path(path)

    def exists(self, session_id: str) -> bool:
        if not is_safe_session_id(session_id):
            return False
        path = self.sessions_dir / f"{session_id}.jsonl"
        if not path.exists():
            return False
        return record_from_path(path).status != "corrupt"


def record_from_path(path: Path) -> SessionRecord:
    session_id = path.stem
    try:
        events = _load_events(path)
    except Exception as exc:  # noqa: BLE001 - catalog 需要隔离单个损坏 session。
        return SessionRecord(
            session_id=session_id,
            title=session_id,
            status="corrupt",
            error=str(exc),
        )

    if not events:
        return SessionRecord(session_id=session_id, title=session_id, status="empty")

    return build_record_from_events(session_id=session_id, events=events)


def _load_events(path: Path) -> list[JournalEnvelope]:
    paths = LansCoderPaths(storage_root=path.parent.parent)
    return JournalStore(paths, path.stem).read_events()


def is_safe_session_id(session_id: str) -> bool:

    return bool(SAFE_SESSION_ID_PATTERN.fullmatch(session_id))


def _validate_session_id(session_id: str) -> None:
    if not is_safe_session_id(session_id):
        raise SessionInvalidIdError(f"invalid session_id: {session_id!r}")


def build_record_from_events(*, session_id: str, events: list[JournalEnvelope]) -> SessionRecord:
    metadata: dict[str, Any] = {}
    message_count = 0
    user_turn_count = 0
    checkpoint_count = 0
    archive_ids: set[str] = set()
    latest_user_input: str | None = None
    latest_assistant_output: str | None = None
    latest_checkpoint_id: str | None = None
    provider: str | None = None
    model: str | None = None

    for event in events:
        if event.kind in {"session.created", "session.metadata_updated"}:
            metadata = merge_metadata_patch(metadata, event.data)

        if event.kind in MESSAGE_EVENT_TYPES:
            message_count += 1

        if event.kind == "message.appended" and event.data.get("role") == "user":
            user_turn_count += 1
            latest_user_input = _preview(_first_text_part_content(event.data))

        if event.kind == "message.appended" and event.data.get("role") == "assistant":
            latest_assistant_output = _preview(_first_text_part_content(event.data))
            message_metadata = _payload_metadata(event.data)
            provider = optional_str(message_metadata.get("provider")) or provider
            model = optional_str(message_metadata.get("model")) or model

        if event.kind == "message.appended" and event.data.get("role") == "tool":
            _collect_archive_ids(event.data, archive_ids)

        if event.kind == "compaction.completed":
            _collect_compaction_archive_ids(event.data, archive_ids)

        if event.kind == "checkpoint.created":
            checkpoint_count += 1
            latest_checkpoint_id = optional_str(event.data.get("id")) or latest_checkpoint_id

    title = optional_str(metadata.get("title")) or latest_user_input or session_id
    metadata["session_id"] = session_id
    return SessionRecord(
        session_id=session_id,
        title=title,
        created_at=events[0].occurred_at,
        updated_at=events[-1].occurred_at,
        workspace=optional_str(metadata.get("workspace")),
        provider=provider,
        model=model,
        message_count=message_count,
        user_turn_count=user_turn_count,
        checkpoint_count=checkpoint_count,
        archive_count=len(archive_ids),
        latest_user_input=latest_user_input,
        latest_assistant_output=latest_assistant_output,
        latest_checkpoint_id=latest_checkpoint_id,
        status="ok",
        metadata=metadata,
    )


def _first_text_part_content(payload: dict[str, Any]) -> str | None:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        return None
    for part in parts:
        if not isinstance(part, dict):
            continue
        if str(part.get("kind") or "") == "text":
            return str(part.get("content") or "")
    return None


def _payload_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _collect_archive_ids(payload: dict[str, Any], archive_ids: set[str]) -> None:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        metadata = part.get("metadata")
        if isinstance(metadata, dict):
            archive_id = optional_str(metadata.get("archive_id"))
            if archive_id:
                archive_ids.add(archive_id)
        if str(part.get("kind") or "") == "archive_placeholder":
            part_id = optional_str(part.get("id"))
            if part_id:
                archive_ids.add(part_id)


def _collect_compaction_archive_ids(payload: dict[str, Any], archive_ids: set[str]) -> None:
    event_payload = payload.get("event")
    if not isinstance(event_payload, dict):
        return
    replacements = event_payload.get("replacements")
    if not isinstance(replacements, list):
        return
    for replacement in replacements:
        if not isinstance(replacement, dict):
            continue
        replacement_part = replacement.get("replacement_part")
        if isinstance(replacement_part, dict):
            _collect_archive_ids({"parts": [replacement_part]}, archive_ids)


def _preview(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if len(normalized) <= PREVIEW_CHARS:
        return normalized
    return normalized[: PREVIEW_CHARS - 1] + "..."


def session_sort_key(record: SessionRecord) -> tuple[str, str]:
    return (record.updated_at or "", record.session_id)
