"""Locked append-only schema v1 JSONL journal storage."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from lanscoder.journal.models import JOURNAL_SCHEMA_VERSION, JournalEnvelope
from lanscoder.journal.recovery import JournalCorruptError, decode_json_line, preserve_tail
from lanscoder.storage.locking import AdvisoryLock
from lanscoder.storage.paths import LansCoderPaths

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,128}")


class JournalStore:
    """A journal store whose every session operation is lock protected."""

    def __init__(self, paths: LansCoderPaths | str | Path, session_id: str | None = None) -> None:
        self.paths = paths if isinstance(paths, LansCoderPaths) else LansCoderPaths(storage_root=paths)
        self.session_id = session_id
        if session_id is not None:
            _validate_session_id(session_id)

    def append(
        self,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        trace_id: str | None = None,
        observation_id: str | None = None,
        parent_observation_id: str | None = None,
        branch_id: str | None = None,
    ) -> JournalEnvelope:
        resolved_session_id = self._resolve_session_id(session_id)
        with self._lock(resolved_session_id):
            events = self._read_locked(resolved_session_id, recover_tail=True)
            next_sequence = len(events) + 1
            if events:
                next_sequence = events[-1].sequence + 1
            envelope = JournalEnvelope.create(
                sequence=next_sequence,
                kind=kind,
                session_id=resolved_session_id,
                data=data,
                event_id=event_id,
                occurred_at=occurred_at,
                trace_id=trace_id,
                observation_id=observation_id,
                parent_observation_id=parent_observation_id,
                branch_id=branch_id,
            )
            self._append_locked(envelope)
            return envelope

    def append_envelope(self, envelope: JournalEnvelope) -> JournalEnvelope:
        """Append an envelope after replacing its sequence with the next one."""
        resolved_session_id = self._resolve_session_id(envelope.session_id)
        if envelope.session_id != resolved_session_id:
            raise ValueError("envelope session_id does not match the store")
        with self._lock(resolved_session_id):
            events = self._read_locked(resolved_session_id, recover_tail=True)
            sequence = events[-1].sequence + 1 if events else 1
            if envelope.sequence != sequence:
                envelope = JournalEnvelope(
                    schema_version=envelope.schema_version,
                    sequence=sequence,
                    event_id=envelope.event_id,
                    occurred_at=envelope.occurred_at,
                    kind=envelope.kind,
                    session_id=envelope.session_id,
                    trace_id=envelope.trace_id,
                    observation_id=envelope.observation_id,
                    parent_observation_id=envelope.parent_observation_id,
                    branch_id=envelope.branch_id,
                    data=envelope.data,
                )
            self._append_locked(envelope)
            return envelope

    def append_event(self, envelope: JournalEnvelope) -> JournalEnvelope:
        return self.append_envelope(envelope)

    def read_events(self, session_id: str | None = None) -> list[JournalEnvelope]:
        resolved_session_id = self._resolve_session_id(session_id)
        try:
            with self._lock(resolved_session_id, shared=True):
                events = self._read_locked(resolved_session_id, recover_tail=False)
        except JournalCorruptError as error:
            if "incomplete record" not in str(error):
                raise
            with self._lock(resolved_session_id):
                return self._read_locked(resolved_session_id, recover_tail=True)

        path = self.paths.session(resolved_session_id)
        raw = path.read_bytes() if path.exists() else b""
        if raw and not raw.endswith(b"\n"):
            with self._lock(resolved_session_id):
                return self._read_locked(resolved_session_id, recover_tail=True)
        return events

    def list_events(self, session_id: str | None = None) -> list[JournalEnvelope]:
        return self.read_events(session_id)

    def session_path(self, session_id: str | None = None) -> Path:
        return self.paths.session(self._resolve_session_id(session_id))

    def _resolve_session_id(self, session_id: str | None) -> str:
        resolved = session_id or self.session_id
        if resolved is None:
            raise ValueError("session_id is required")
        _validate_session_id(resolved)
        return resolved

    def _lock(self, session_id: str, *, shared: bool = False) -> AdvisoryLock:
        return AdvisoryLock(self.paths.session_lock(session_id), shared=shared)

    def _read_locked(self, session_id: str, *, recover_tail: bool) -> list[JournalEnvelope]:
        path = self.paths.session(session_id)
        if not path.exists():
            return []
        raw = path.read_bytes()
        if not raw:
            return []
        lines = raw.splitlines(keepends=True)
        tail = b""
        tail_start = len(raw)
        if lines and not lines[-1].endswith(b"\n"):
            tail = lines.pop()
            tail_start = len(raw) - len(tail)

        events: list[JournalEnvelope] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                raise JournalCorruptError(session_id, f"blank record at line {line_number}")
            value = decode_json_line(line.rstrip(b"\r\n"), session_id=session_id, line_number=line_number)
            try:
                event = JournalEnvelope.from_dict(value)  # type: ignore[arg-type]
            except (KeyError, TypeError, ValueError) as error:
                raise JournalCorruptError(session_id, f"invalid schema at line {line_number}: {error}") from error
            self._validate_sequence(event, session_id, len(events) + 1)
            events.append(event)

        if tail:
            try:
                value = json.loads(tail.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if value is not None:
                try:
                    tail_event = JournalEnvelope.from_dict(value)
                except (KeyError, TypeError, ValueError) as error:
                    raise JournalCorruptError(session_id, f"invalid schema in final record: {error}") from error
                self._validate_sequence(tail_event, session_id, len(events) + 1)
                if recover_tail:
                    with path.open("ab") as handle:
                        handle.write(b"\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                events.append(tail_event)
                return events
            if not recover_tail:
                raise JournalCorruptError(session_id, "journal ends with an incomplete record")
            recovery = preserve_tail(
                path,
                self.paths.recovery_tails,
                session_id=session_id,
                start_byte=tail_start,
                end_byte=len(raw),
            )
            with path.open("r+b") as handle:
                handle.truncate(tail_start)
                handle.flush()
                os.fsync(handle.fileno())
            recovery_event = JournalEnvelope.create(
                sequence=(events[-1].sequence + 1 if events else 1),
                kind="journal.recovered",
                session_id=session_id,
                data=recovery.to_event_data(),
            )
            self._append_locked(recovery_event)
            events.append(recovery_event)
        return events

    def _append_locked(self, envelope: JournalEnvelope) -> None:
        path = self.paths.session(envelope.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _validate_sequence(event: JournalEnvelope, session_id: str, expected: int) -> None:
        if event.session_id != session_id:
            raise JournalCorruptError(session_id, f"record session_id mismatch at sequence {expected}")
        if event.sequence != expected:
            raise JournalCorruptError(session_id, f"expected sequence {expected}, got {event.sequence}")
        if event.schema_version != JOURNAL_SCHEMA_VERSION:
            raise JournalCorruptError(session_id, f"unsupported schema_version {event.schema_version}")


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or _SAFE_COMPONENT.fullmatch(session_id) is None:
        raise ValueError("session_id must contain only letters, digits, underscores, or hyphens")


def read_all_sessions(paths: LansCoderPaths) -> dict[str, list[JournalEnvelope]]:
    """Read all session journals in stable filename order."""
    result: dict[str, list[JournalEnvelope]] = {}
    for path in sorted(paths.sessions.glob("*.jsonl")):
        result[path.stem] = JournalStore(paths, path.stem).read_events()
    return result
