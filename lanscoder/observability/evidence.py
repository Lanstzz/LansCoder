"""JSON normalization and bounded query fields without provider dependencies."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

METADATA_FIELDS = frozenset({"team", "operation", "component", "environment", "role", "kind", "request_id", "attempt_index", "project_id"})
PARAMETER_FIELDS = frozenset({"temperature", "max_tokens", "max_completion_tokens", "reasoning_effort", "tool_choice"})
STREAM_FIELDS = frozenset(
    {
        "message_started",
        "reasoning_delta",
        "text_delta",
        "tool_call_started",
        "tool_call_delta",
        "tool_call_completed",
        "message_completed",
        "error",
        "delta_count",
        "first_output_at",
        "first_output_ms",
    }
)


def json_safe(value: Any, *, _seen: frozenset[int] = frozenset()) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    summary = {"type": type(value).__name__, "summary": "not JSON serializable"}
    if id(value) in _seen:
        return summary
    seen = _seen | {id(value)}
    try:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return json_safe(model_dump(mode="json"), _seen=seen)
        if isinstance(value, Mapping):
            return {key: json_safe(item, _seen=seen) for key, item in value.items() if isinstance(key, str)}
        if isinstance(value, (list, tuple)):
            return [json_safe(item, _seen=seen) for item in value]
        if is_dataclass(value) and not isinstance(value, type):
            return {field.name: json_safe(getattr(value, field.name), _seen=seen) for field in fields(value) if field.name != "raw"}
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return json_safe(to_dict(), _seen=seen)
    except Exception:
        return summary
    return summary


def serializable_raw(value: Any) -> Any | None:
    """Return raw evidence only if the complete object survives strict JSON."""
    try:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            value = model_dump(mode="json")
        elif not isinstance(value, (dict, list)):
            return None
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except Exception:
        return None


def bounded_fields(value: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if item is None or isinstance(item, (bool, int)) or isinstance(item, float) and math.isfinite(item):
            result[key] = item
        elif isinstance(item, str) and len(item) <= 256:
            result[key] = item
    return result


def bounded_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and 0 < len(item) <= 64))[:32]
