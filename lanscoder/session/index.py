from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lanscoder.journal.models import JournalEnvelope
from lanscoder.journal.store import JournalStore
from lanscoder.session.access import PRIMARY_KIND
from lanscoder.session.catalog import session_sort_key
from lanscoder.session.models import SessionRecord
from lanscoder.storage import index_lock
from lanscoder.storage.paths import LansCoderPaths

INDEX_VERSION = 1


class SessionIndex:
    """Maintain the disposable, globally indexed session projection.

    Session journals are always snapshotted before the index lock is acquired.
    This keeps the session and index advisory locks independent and preserves
    the documented lock order for both writers and rebuilds.
    """

    def __init__(self, root: str | Path, *, journal: Any | None = None, project_id: str | None = None) -> None:
        self.root = Path(root)
        self.paths = LansCoderPaths(storage_root=self.root)
        self.path = self.paths.indexes / "sessions.json"
        self.journal = journal
        self.project_id = project_id

    def update_event(self, event: Any) -> None:
        """Merge one journal watermark without making persistence depend on it."""
        try:
            session_id = _event_session_id(event)
            events = self._load_session_events(session_id)
            if not events:
                return
            record = _build_record_or_corrupt(session_id, events)
            with index_lock(self.paths):
                data = self._load_data()
                data["sessions"][session_id] = _record_entry(record, events)
                self._write_data(data)
        except Exception:
            # The journal is the source of truth; a later list/rebuild repairs
            # an unavailable or partially written materialized index.
            return

    def list_records(self, *, project_id: str | None = None, kind: str | None = PRIMARY_KIND) -> list[SessionRecord]:
        resolved_project_id = self.project_id if project_id is None else project_id
        if self._needs_rebuild():
            self.rebuild()
        try:
            with index_lock(self.paths):
                data = self._load_data()
        except Exception:
            return []
        if not _index_data_is_complete(data):
            return []
        records: list[SessionRecord] = []
        for item in data["sessions"].values():
            if _record_matches(item, project_id=resolved_project_id, kind=kind):
                try:
                    records.append(_record_from_dict(item))
                except (TypeError, ValueError):
                    return []
        return sorted(records, key=session_sort_key, reverse=True)

    def rebuild_session(self, session_id: str) -> None:
        """Replace one entry after its session snapshot lock is released."""
        path = self.paths.session(session_id)
        if path.exists():
            try:
                events = self._load_session_events(session_id)
            except Exception as exc:  # noqa: BLE001 - preserve a diagnostic entry.
                events = []
                record = SessionRecord(session_id=session_id, title=session_id, status="corrupt", error=str(exc))
            else:
                record = _build_record_or_corrupt(session_id, events) if events else None
        else:
            record = None
            events = []

        try:
            with index_lock(self.paths):
                data = self._load_data()
                if record is None:
                    data["sessions"].pop(session_id, None)
                else:
                    data["sessions"][session_id] = _record_entry(record, events)
                self._write_data(data)
        except Exception:
            return

    def prune_empty(self, exclude: set[str] | None = None) -> int:
        import os as _os

        sessions_dir = self.paths.sessions
        if not sessions_dir.exists():
            return 0

        skip = exclude or set()
        try:
            with index_lock(self.paths):
                data = self._load_data()
                sessions = data["sessions"]
                to_prune = [
                    sid
                    for sid, item in sessions.items()
                    if sid not in skip and isinstance(item, dict) and item.get("user_turn_count", 0) == 0
                ]
                for sid in to_prune:
                    sessions.pop(sid, None)
                    try:
                        _os.remove(sessions_dir / f"{sid}.jsonl")
                    except FileNotFoundError:
                        pass
                if to_prune:
                    self._write_data(data)
                return len(to_prune)
        except Exception:
            return 0

    def rebuild(self) -> None:
        """Read stable, one-session snapshots, then atomically replace the index."""
        snapshots = self._snapshot_records()
        data = _empty_data()
        data["sessions"] = {
            session_id: _record_entry(record, events)
            for session_id, record, events in snapshots
            if record is not None
        }
        try:
            with index_lock(self.paths):
                self._write_data(data)
        except Exception:
            return

    def _snapshot_records(self) -> list[tuple[str, SessionRecord | None, list[Any]]]:
        snapshots: list[tuple[str, SessionRecord | None, list[Any]]] = []
        for session_id in self._journal_session_ids():
            try:
                events = self._load_session_events(session_id)
            except Exception as exc:  # noqa: BLE001 - isolate one corrupt session.
                snapshots.append(
                    (
                        session_id,
                        SessionRecord(session_id=session_id, title=session_id, status="corrupt", error=str(exc)),
                        [],
                    )
                )
                continue
            if not events:
                continue
            snapshots.append((session_id, _build_record_or_corrupt(session_id, events), events))
        return snapshots

    def _needs_rebuild(self) -> bool:
        if not self.path.exists():
            return True
        try:
            with index_lock(self.paths):
                data = self._load_data()
            if not _index_data_is_complete(data):
                return True
            current_ids = set(self._journal_session_ids())
            if current_ids != set(data["sessions"]):
                return True
            for session_id in sorted(current_ids):
                try:
                    events = self._load_session_events(session_id)
                except Exception:
                    return True
                expected = events[-1].sequence if events else 0
                if data["sessions"][session_id].get("last_sequence") != expected:
                    return True
            return False
        except Exception:
            return True

    def _load_data(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_data()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - let callers enter rebuild path.
            raise ValueError("session index is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            raise ValueError("session index has an unsupported shape or version")
        if not isinstance(data.get("sessions"), dict):
            raise ValueError("session index sessions must be an object")
        return data

    def _write_data(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".sessions.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            _fsync_directory(self.path.parent)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

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
        return JournalStore(self.paths, session_id).read_events()

    def _journal_session_ids(self) -> list[str]:
        if self.journal is not None:
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
        return sorted(path.stem for path in self.paths.sessions.glob("*.jsonl"))


def _empty_data() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "sessions": {}}


def _record_entry(record: SessionRecord, events: list[Any]) -> dict[str, Any]:
    value = asdict(record)
    value["last_sequence"] = max((_event_sequence(event, index + 1) for index, event in enumerate(events)), default=0)
    return value


def _record_from_dict(data: dict[str, Any]) -> SessionRecord:
    _validate_index_entry(data)
    return SessionRecord(
        session_id=data["session_id"],
        title=data["title"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        workspace=data["workspace"],
        provider=data["provider"],
        model=data["model"],
        message_count=data["message_count"],
        user_turn_count=data["user_turn_count"],
        checkpoint_count=data["checkpoint_count"],
        archive_count=data["archive_count"],
        latest_user_input=data["latest_user_input"],
        latest_assistant_output=data["latest_assistant_output"],
        latest_checkpoint_id=data["latest_checkpoint_id"],
        status=data["status"],
        error=data["error"],
        metadata=dict(data["metadata"]),
    )


def _event_session_id(event: Any) -> str:
    if isinstance(event, dict):
        value = event.get("data")
        return str(event.get("session_id") or (value.get("session_id") if isinstance(value, dict) else "") or "")
    return str(getattr(event, "session_id", ""))


def _build_record_or_corrupt(session_id: str, events: list[Any]) -> SessionRecord:
    try:
        return _build_record(session_id, events)
    except Exception as exc:  # noqa: BLE001 - one bad session must not hide others.
        return SessionRecord(session_id=session_id, title=session_id, status="corrupt", error=str(exc))


def _build_record(session_id: str, events: list[Any]) -> SessionRecord:
    if not all(isinstance(event, JournalEnvelope) for event in events):
        raise TypeError("session index requires schema-v1 JournalEnvelope records")
    from lanscoder.session.catalog import build_record_from_events

    return build_record_from_events(session_id=session_id, events=events)


def _record_matches(data: dict[str, Any], *, project_id: str | None, kind: str | None) -> bool:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    actual_kind = metadata.get("kind", PRIMARY_KIND)
    actual_project = metadata.get("project_id")
    return (kind is None or actual_kind == kind) and (project_id is None or actual_project == project_id)


def _index_data_is_complete(data: dict[str, Any]) -> bool:
    if data.get("version") != INDEX_VERSION or not isinstance(data.get("sessions"), dict):
        return False
    for session_id, item in data["sessions"].items():
        if not isinstance(item, dict) or item.get("session_id") != session_id:
            return False
        try:
            _validate_index_entry(item)
        except (TypeError, ValueError):
            return False
    return True


def _validate_index_entry(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TypeError("index record must be an object")
    fields = (
        "session_id",
        "title",
        "created_at",
        "updated_at",
        "workspace",
        "provider",
        "model",
        "message_count",
        "user_turn_count",
        "checkpoint_count",
        "archive_count",
        "latest_user_input",
        "latest_assistant_output",
        "latest_checkpoint_id",
        "status",
        "error",
        "metadata",
        "last_sequence",
    )
    missing = [field for field in fields if field not in data]
    if missing:
        raise ValueError(f"index record is missing fields: {', '.join(missing)}")
    for field in ("session_id", "title", "status"):
        if not isinstance(data[field], str) or not data[field]:
            raise TypeError(f"index record {field} must be a non-empty string")
    for field in (
        "created_at",
        "updated_at",
        "workspace",
        "provider",
        "model",
        "latest_user_input",
        "latest_assistant_output",
        "latest_checkpoint_id",
        "error",
    ):
        if data[field] is not None and not isinstance(data[field], str):
            raise TypeError(f"index record {field} must be a string or null")
    for field in ("message_count", "user_turn_count", "checkpoint_count", "archive_count"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError(f"index record {field} must be a non-negative integer")
    if not isinstance(data["metadata"], dict):
        raise TypeError("index record metadata must be an object")
    last_sequence = data["last_sequence"]
    if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < 0:
        raise TypeError("index record last_sequence must be a non-negative integer")
    if data["status"] != "corrupt" and last_sequence < 1:
        raise ValueError("healthy index records require a positive last_sequence")


def _event_sequence(event: Any, fallback: int) -> int:
    value = event.get("sequence") if isinstance(event, dict) else getattr(event, "sequence", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
