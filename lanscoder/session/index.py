from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lanscoder.context.events import SessionEvent
from lanscoder.session.access import PRIMARY_KIND
from lanscoder.session.catalog import session_sort_key
from lanscoder.session.models import SessionRecord
from lanscoder.utils.text import optional_str

INDEX_VERSION = 1
_INDEX_LOCK = threading.RLock()


class SessionIndex:

    def __init__(self, root: str | Path, *, journal: Any | None = None, project_id: str | None = None) -> None:
        self.root = Path(root)
        self.path = self.root / "session_index.json"
        self.journal = journal
        self.project_id = project_id

    def update_event(self, event: Any) -> None:
        with _INDEX_LOCK:
            data = self._load_data()
            session_id = _event_session_id(event)
            events = self._load_session_events(session_id)
            if not events:
                return
            try:
                record = _build_record(session_id, events)
            except Exception as exc:  # noqa: BLE001 - index must not block event persistence.
                record = SessionRecord(session_id=session_id, title=session_id, status="corrupt", error=str(exc))
            if _record_is_primary(record, project_id=self.project_id):
                data["sessions"][session_id] = _record_to_dict(record)
            else:
                data["sessions"].pop(session_id, None)
            self._write_data(data)

    def list_records(self, *, project_id: str | None = None, kind: str = PRIMARY_KIND) -> list[SessionRecord]:
        if not self.path.exists():
            self.rebuild()
        elif self.journal is None:
            self._reconcile_missing_files()
        data = self._load_data()
        resolved_project_id = self.project_id if project_id is None else project_id
        records = [
            _record_from_dict(item)
            for item in data.get("sessions", {}).values()
            if isinstance(item, dict) and _record_matches(item, project_id=resolved_project_id, kind=kind)
        ]
        return sorted(records, key=session_sort_key, reverse=True)

    def rebuild_session(self, session_id: str) -> None:
        from lanscoder.session.catalog import record_from_path

        path = self.root / "sessions" / f"{session_id}.jsonl"
        if not path.exists():
            with _INDEX_LOCK:
                data = self._load_data()
                data["sessions"].pop(session_id, None)
                self._write_data(data)
            return

        record = record_from_path(path)
        with _INDEX_LOCK:
            data = self._load_data()
            if _record_is_primary(record, project_id=self.project_id):
                data["sessions"][session_id] = _record_to_dict(record)
            else:
                data["sessions"].pop(session_id, None)
            self._write_data(data)

    def prune_empty(self, exclude: set[str] | None = None) -> int:
        import os

        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return 0

        skip = exclude or set()

        with _INDEX_LOCK:
            data = self._load_data()
            sessions = data["sessions"]
            to_prune: list[str] = []
            for sid, item in sessions.items():
                if sid in skip:
                    continue
                if isinstance(item, dict) and item.get("user_turn_count", 0) == 0:
                    to_prune.append(sid)

            for sid in to_prune:
                sessions.pop(sid, None)
                path = sessions_dir / f"{sid}.jsonl"
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

            if to_prune:
                self._write_data(data)

            return len(to_prune)

    def rebuild(self) -> None:
        with _INDEX_LOCK:
            sessions_dir = self.root / "sessions"
            data = _empty_data()
            if self.journal is not None:
                for session_id in self._journal_session_ids():
                    events = self._load_session_events(session_id)
                    if not events:
                        continue
                    try:
                        record = _build_record(session_id, events)
                    except Exception as exc:  # noqa: BLE001 - a bad session is indexed as corrupt.
                        record = SessionRecord(session_id=session_id, title=session_id, status="corrupt", error=str(exc))
                    if _record_is_primary(record, project_id=self.project_id):
                        data["sessions"][session_id] = _record_to_dict(record)
            elif sessions_dir.exists():
                from lanscoder.session.catalog import record_from_path

                for path in sessions_dir.glob("*.jsonl"):
                    record = record_from_path(path)
                    if _record_is_primary(record, project_id=self.project_id):
                        data["sessions"][record.session_id] = _record_to_dict(record)
            self._write_data(data)

    def _reconcile_missing_files(self) -> None:
        from lanscoder.session.catalog import record_from_path

        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return
        with _INDEX_LOCK:
            data = self._load_data()
            sessions = data["sessions"]
            changed = False
            for path in sessions_dir.glob("*.jsonl"):
                if path.stem in sessions:
                    continue
                record = record_from_path(path)
                if _record_is_primary(record, project_id=self.project_id):
                    sessions[record.session_id] = _record_to_dict(record)
                    changed = True
            if changed:
                self._write_data(data)

    def _load_data(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_data()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt index can be rebuilt.
            return _empty_data()
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return _empty_data()
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            data["sessions"] = {}
        return data

    def _write_data(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def _load_session_events(self, session_id: str) -> list[Any]:
        if self.journal is not None:
            if isinstance(self.journal, dict):
                value = self.journal.get(session_id, [])
            else:
                for name in ("list_events", "read_events", "events_for"):
                    method = getattr(self.journal, name, None)
                    if method is not None:
                        value = method(session_id)
                        break
                else:
                    value = []
            return list(value or [])
        path = self.root / "sessions" / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    events.append(SessionEvent.from_dict(json.loads(line)))
        return events

    def _journal_session_ids(self) -> list[str]:
        if isinstance(self.journal, dict):
            return sorted(str(session_id) for session_id in self.journal)
        for name in ("session_ids", "list_session_ids", "sessions"):
            value = getattr(self.journal, name, None)
            if callable(value):
                value = value()
            if value is not None:
                return sorted(str(session_id) for session_id in value)
        paths = getattr(self.journal, "paths", None)
        sessions_dir = getattr(paths, "sessions", None)
        if sessions_dir is not None:
            return sorted(path.stem for path in Path(sessions_dir).glob("*.jsonl"))
        return []


def _empty_data() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "sessions": {}}


def _record_to_dict(record: SessionRecord) -> dict[str, Any]:
    return asdict(record)


def _record_from_dict(data: dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        session_id=str(data.get("session_id") or ""),
        title=str(data.get("title") or data.get("session_id") or ""),
        created_at=optional_str(data.get("created_at")),
        updated_at=optional_str(data.get("updated_at")),
        workspace=optional_str(data.get("workspace")),
        provider=optional_str(data.get("provider")),
        model=optional_str(data.get("model")),
        message_count=int(data.get("message_count") or 0),
        user_turn_count=int(data.get("user_turn_count") or 0),
        checkpoint_count=int(data.get("checkpoint_count") or 0),
        archive_count=int(data.get("archive_count") or 0),
        latest_user_input=optional_str(data.get("latest_user_input")),
        latest_assistant_output=optional_str(data.get("latest_assistant_output")),
        latest_checkpoint_id=optional_str(data.get("latest_checkpoint_id")),
        status=str(data.get("status") or "ok"),
        error=optional_str(data.get("error")),
        metadata=dict(data.get("metadata") or {}),
    )


def _event_session_id(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("session_id") or event.get("data", {}).get("session_id") or "")
    return str(getattr(event, "session_id", ""))


def _build_record(session_id: str, events: list[Any]) -> SessionRecord:
    if all(isinstance(event, SessionEvent) for event in events):
        from lanscoder.session.catalog import build_record_from_events

        return build_record_from_events(session_id=session_id, events=events)

    from lanscoder.session.projection import active_projection

    projected = active_projection(events)
    metadata: dict[str, Any] = {}
    user_turn_count = 0
    message_count = 0
    latest_user_input: str | None = None
    for event in projected:
        kind = _event_value(event, "kind", "type")
        data = _event_mapping(event, "data", "payload")
        if kind in {"session.created", "session.metadata_updated", "session_created", "session_metadata_updated"}:
            metadata.update(data)
        if kind in {"message.appended", "user_message", "assistant_message", "tool_result"}:
            message_count += 1
        if kind in {"message.appended", "user_message"} and (
            data.get("role") == "user" or kind == "user_message"
        ):
            user_turn_count += 1
            latest_user_input = _message_preview(data)
    metadata.setdefault("session_id", session_id)
    title = str(metadata.get("title") or latest_user_input or session_id)
    return SessionRecord(
        session_id=session_id,
        title=title,
        created_at=_event_value(events[0], "occurred_at", "created_at"),
        updated_at=_event_value(events[-1], "occurred_at", "created_at"),
        workspace=_optional_text(metadata.get("workspace")),
        message_count=message_count,
        user_turn_count=user_turn_count,
        latest_user_input=latest_user_input,
        status="ok",
        metadata=metadata,
    )


def _record_is_primary(record: SessionRecord, *, project_id: str | None) -> bool:
    return _record_matches(_record_to_dict(record), project_id=project_id, kind=PRIMARY_KIND)


def _record_matches(data: dict[str, Any], *, project_id: str | None, kind: str) -> bool:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    actual_kind = metadata.get("kind", PRIMARY_KIND)
    actual_project = metadata.get("project_id")
    return actual_kind == kind and (project_id is None or actual_project == project_id)


def _event_value(event: Any, *names: str) -> str | None:
    for name in names:
        value = event.get(name) if isinstance(event, dict) else getattr(event, name, None)
        if value is not None:
            return str(value)
    return None


def _event_mapping(event: Any, *names: str) -> dict[str, Any]:
    for name in names:
        value = event.get(name) if isinstance(event, dict) else getattr(event, name, None)
        if isinstance(value, dict):
            return dict(value)
    return {}


def _message_preview(data: dict[str, Any]) -> str | None:
    if isinstance(data.get("content"), str):
        return data["content"]
    parts = data.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                return str(part.get("content") or "")
    return None


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None
