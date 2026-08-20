from __future__ import annotations

from typing import Any

RESERVED_METADATA_KEYS = frozenset({"session_id"})


def merge_metadata_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:

    merged = dict(current)
    for key, value in patch.items():
        key = str(key)
        if key in RESERVED_METADATA_KEYS:
            continue
        if value is not None:
            merged[key] = value
    return merged


def metadata_without_reserved_keys(metadata: dict[str, Any]) -> dict[str, Any]:

    return {str(key): value for key, value in metadata.items() if str(key) not in RESERVED_METADATA_KEYS}
