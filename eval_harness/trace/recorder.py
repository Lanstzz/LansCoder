"""Canonical JSONL trace recorder with an integrity footer."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval_harness.schema.models import SCHEMA_VERSION
from eval_harness.trace.redaction import Redactor


class TraceRecorder:
    """Collect fresh runtime facts and persist them as one portable JSONL trace."""

    def __init__(self, output_dir: str | Path, *, redactor: Redactor) -> None:
        self.output_dir = Path(output_dir)
        self.redactor = redactor
        self.events: list[dict[str, object]] = []

    def record(self, event_type: str, data: Any | None = None) -> dict[str, object]:
        event = {
            "schema_version": SCHEMA_VERSION,
            "sequence": len(self.events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "data": self.redactor.redact(_json_value(data if data is not None else {})),
        }
        self.events.append(event)
        return event

    def write(self) -> Path:
        """Write ``trace.jsonl`` and append its checksum as the final event."""

        if not self.events or self.events[-1]["type"] != "trace_integrity":
            digest = trace_digest(self.events)
            self.record(
                "trace_integrity",
                {
                    "algorithm": "sha256",
                    "event_count": len(self.events),
                    "digest": digest,
                },
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        trace_path = self.output_dir / "trace.jsonl"
        trace_path.write_text("\n".join(_encode(event) for event in self.events) + "\n", encoding="utf-8")
        return trace_path


def trace_digest(events: list[dict[str, object]]) -> str:
    """Digest a trace prefix exactly as it is written to the JSONL file."""

    payload = "\n".join(_encode(event) for event in events)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
