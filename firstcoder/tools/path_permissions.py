"""Shared permission helpers for path-reading tools."""

from __future__ import annotations

from typing import Any


def read_path_target(arguments: dict[str, Any]) -> str:
    return str(arguments.get("path") or ".")


def read_multi_target(arguments: dict[str, Any]) -> str:
    paths = arguments.get("paths")
    if not isinstance(paths, list):
        return ""
    return "\n".join(str(path) for path in paths)
