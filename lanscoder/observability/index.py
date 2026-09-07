"""Disposable, rebuildable materialized trace summaries."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from lanscoder.journal import JournalStore
from lanscoder.storage import LansCoderPaths, PayloadStore, index_lock

from .models import TraceStatus, TraceSummary, project_trace


TRACE_INDEX_VERSION = 1


class JournalTraceIndex:
    """Maintain trace summaries without making the index a source of truth."""

    def __init__(self, paths: LansCoderPaths | str | Path, *, journal: Any | None = None, payload_store: Any | None = None) -> None:
        self.paths = paths if isinstance(paths, LansCoderPaths) else LansCoderPaths(storage_root=paths)
        self.path = self.paths.indexes / "traces.json"
        self.journal = journal
        self.payload_store = PayloadStore(self.paths) if payload_store is None else payload_store

    def update_event(self, event: Any) -> None:
        """Project one journal session after its append has completed."""
        session_id = _event_value(event, "session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("trace index events require session_id")
        for _ in range(3):
            try:
                with index_lock(self.paths):
                    before = self._cache_bytes()
                    data = self._load_data()
            except ValueError:
                self.rebuild()
                return
            events = self._load_session_events(session_id)
            summaries = _project_session(events, self.payload_store)
            if data["sessions"].get(session_id, 0) > _last_sequence(events):
                self.rebuild()
                return
            with index_lock(self.paths):
                if self._cache_bytes() != before:
                    continue
                _replace_session(data, session_id, events, summaries)
                self._write_data(data)
                return
        raise RuntimeError("trace index changed repeatedly during update")

    def list_summaries(self) -> list[TraceSummary]:
        if self._needs_rebuild():
            self.rebuild()
        with index_lock(self.paths):
            data = self._load_data()
        return sorted(
            (_summary_from_dict(item["summary"]) for item in data["traces"].values()),
            key=lambda summary: (summary.started_at or "", summary.trace_id),
            reverse=True,
        )

    def rebuild(self) -> None:
        for _ in range(3):
            with index_lock(self.paths):
                before = self._cache_bytes()
            data = _empty_data()
            for session_id in self._journal_session_ids():
                events = self._load_session_events(session_id)
                _replace_session(data, session_id, events, _project_session(events, self.payload_store))
            with index_lock(self.paths):
                # Re-snapshot if another writer published while session locks
                # were released. Never overwrite its newer watermarks.
                if self._cache_bytes() != before:
                    continue
                self._write_data(data)
                return
        raise RuntimeError("trace index changed repeatedly during rebuild")

    def _needs_rebuild(self) -> bool:
        if not self.path.exists():
            return True
        try:
            with index_lock(self.paths):
                data = self._load_data()
            session_ids = self._journal_session_ids()
            if set(session_ids) != set(data["sessions"]):
                return True
            expected = _empty_data()
            for session_id in session_ids:
                events = self._load_session_events(session_id)
                if data["sessions"][session_id] != _last_sequence(events):
                    return True
                _replace_session(expected, session_id, events, _project_session(events, self.payload_store))
            # Payload integrity may change without advancing a journal watermark.
            return expected != data
        except Exception:
            return True

    def _cache_bytes(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _load_data(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as error:
            raise ValueError("trace index is not valid JSON") from error
        if not isinstance(data, dict) or data.get("version") != TRACE_INDEX_VERSION:
            raise ValueError("trace index has an unsupported version")
        if not isinstance(data.get("sessions"), dict) or not isinstance(data.get("traces"), dict):
            raise ValueError("trace index has an invalid shape")
        for session_id, sequence in data["sessions"].items():
            if not isinstance(session_id, str) or not session_id or type(sequence) is not int or sequence < 0:
                raise ValueError("trace index has an invalid session watermark")
        for trace_id, item in data["traces"].items():
            if not isinstance(trace_id, str) or not isinstance(item, dict) or item.get("session_id") not in data["sessions"]:
                raise ValueError("trace index has an invalid trace entry")
            summary = _summary_from_dict(item.get("summary"))
            if summary.trace_id != trace_id or summary.session_id != item["session_id"]:
                raise ValueError("trace index has mismatched identity")
        return data

    def _write_data(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".traces.", suffix=".tmp", dir=self.path.parent)
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
                return list(self.journal.get(session_id, []) or [])
            for name in ("read_events", "list_events", "events_for"):
                method = getattr(self.journal, name, None)
                if method is not None:
                    return list(method(session_id) or [])
            return []
        return JournalStore(self.paths, session_id).read_events()

    def _journal_session_ids(self) -> list[str]:
        if self.journal is not None:
            if isinstance(self.journal, dict):
                return sorted(self.journal)
            for name in ("session_ids", "list_session_ids", "sessions"):
                value = getattr(self.journal, name, None)
                if callable(value):
                    value = value()
                if value is not None:
                    return sorted(str(item) for item in value)
            journal_paths = getattr(getattr(self.journal, "paths", None), "sessions", None)
            if journal_paths is not None:
                return sorted(path.stem for path in Path(journal_paths).glob("*.jsonl"))
            return []
        return sorted(path.stem for path in self.paths.sessions.glob("*.jsonl"))


def _empty_data() -> dict[str, Any]:
    return {"version": TRACE_INDEX_VERSION, "sessions": {}, "traces": {}}


def _replace_session(data: dict[str, Any], session_id: str, events: list[Any], summaries: list[TraceSummary]) -> None:
    data["sessions"][session_id] = _last_sequence(events)
    for trace_id, item in list(data["traces"].items()):
        if item.get("session_id") == session_id:
            del data["traces"][trace_id]
    for summary in summaries:
        data["traces"][summary.trace_id] = {
            "session_id": session_id,
            "last_sequence": _last_sequence(events),
            "summary": summary.to_dict(),
        }


def _project_session(events: list[Any], payload_store: Any | None) -> list[TraceSummary]:
    return [project_trace(events, trace_id, payload_store=payload_store).to_summary() for trace_id in sorted(_trace_ids(events))]


def _trace_ids(events: list[Any]) -> set[str]:
    return {trace_id for event in events if _event_value(event, "kind") == "trace.started" for trace_id in [_event_value(event, "trace_id")] if isinstance(trace_id, str)}


def _last_sequence(events: list[Any]) -> int:
    return max((_event_value(event, "sequence") or 0 for event in events), default=0)


def _summary_from_dict(value: Any) -> TraceSummary:
    if not isinstance(value, dict):
        raise ValueError("trace index summary must be an object")
    status = TraceStatus(value["status"])
    return TraceSummary(
        trace_id=_required_string(value, "trace_id"),
        session_id=_required_string(value, "session_id"),
        branch_id=value.get("branch_id"),
        status=status,
        incomplete=bool(value["incomplete"]),
        started_at=value.get("started_at"),
        ended_at=value.get("ended_at"),
        duration_ms=value.get("duration_ms"),
        provider=value.get("provider"),
        model=value.get("model"),
        tool_name=value.get("tool_name"),
        total_tokens=value.get("total_tokens"),
        usage_details=dict(value.get("usage_details", {})),
        parameters=dict(value.get("parameters", {})),
        has_error=bool(value.get("has_error", False)),
        observation_count=int(value.get("observation_count", 0)),
        observation_counts=dict(value.get("observation_counts", {})),
        metadata=dict(value.get("metadata", {})),
        tags=tuple(value.get("tags", [])),
    )


def _required_string(value: dict[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ValueError(f"trace index summary requires {name}")
    return result


def _event_value(event: Any, name: str) -> Any:
    return event.get(name) if isinstance(event, dict) else getattr(event, name, None)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["JournalTraceIndex", "TRACE_INDEX_VERSION"]
