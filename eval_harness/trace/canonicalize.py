"""Drop non-semantic trace values before golden comparisons."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_VOLATILE_FIELDS = frozenset(
    {
        "timestamp",
        "created_at",
        "elapsed_ms",
        "elapsed_seconds",
        "duration_ms",
        "run_id",
        "request_id",
        "provider_request_id",
        "digest",
    }
)
_VOLATILE_TEXT_IDS = re.compile(r"\b(?:msg|part)_[0-9a-f]{8,}\b")


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL trace and reject non-object entries."""

    events: list[dict[str, Any]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"trace line {number} must be an object")
        events.append(value)
    return events


def canonicalize_trace(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable event projections suitable for deterministic golden tests."""

    return [
        {
            "schema_version": event.get("schema_version"),
            "sequence": event.get("sequence"),
            "type": event.get("type"),
            "data": _canonical_value(event.get("data", {})),
        }
        for event in events
    ]


def canonical_json(events: list[dict[str, Any]]) -> str:
    return json.dumps(canonicalize_trace(events), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items() if str(key) not in _VOLATILE_FIELDS}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, str):
        return _VOLATILE_TEXT_IDS.sub("<RUNTIME_ID>", value)
    return value
