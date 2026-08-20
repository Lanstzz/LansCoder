from __future__ import annotations

import json
from typing import Any


def dumps_json(value: Any) -> str:

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads_json(value: str) -> Any:

    return json.loads(value)


def loads_json_object(value: str) -> dict[str, Any] | str:

    if not value:
        return {}

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value

    if isinstance(parsed, dict):
        return parsed
    return value
