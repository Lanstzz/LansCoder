"""Tail recovery and corruption diagnostics for JSONL journals."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class JournalCorruptError(ValueError):
    """The journal contains damage that must not be silently repaired."""

    status = "corrupt"

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"journal for {session_id} is corrupt: {reason}")


@dataclass(frozen=True, slots=True)
class TailRecovery:
    session_id: str
    start_byte: int
    end_byte: int
    sha256: str
    evidence_path: Path
    recovered_at: str

    def to_event_data(self) -> dict[str, object]:
        return {
            "tail_sha256": self.sha256,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "recovered_at": self.recovered_at,
            "evidence_path": str(self.evidence_path),
        }


def preserve_tail(
    journal_path: Path,
    recovery_dir: Path,
    *,
    session_id: str,
    start_byte: int,
    end_byte: int,
) -> TailRecovery:
    """Copy a damaged tail to evidence storage before it is truncated."""
    with journal_path.open("rb") as handle:
        handle.seek(start_byte)
        tail = handle.read(end_byte - start_byte)
    digest = hashlib.sha256(tail).hexdigest()
    recovery_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = recovery_dir / f"{session_id}-{start_byte}-{end_byte}-{digest}.tail"
    if not evidence_path.exists():
        descriptor = os.open(evidence_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(tail)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            evidence_path.unlink(missing_ok=True)
            raise
    return TailRecovery(
        session_id=session_id,
        start_byte=start_byte,
        end_byte=end_byte,
        sha256=digest,
        evidence_path=evidence_path,
        recovered_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )


def decode_json_line(raw_line: bytes, *, session_id: str, line_number: int) -> dict[str, object]:
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JournalCorruptError(session_id, f"invalid JSON at line {line_number}") from error
    if not isinstance(value, dict):
        raise JournalCorruptError(session_id, f"record at line {line_number} is not an object")
    return value
