"""Portable, versioned data models used by the evaluation harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_MODES = frozenset({"interaction_replay", "fresh_model"})


class ManifestError(ValueError):
    """A portable case manifest is missing a required field or is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderTapeResponse:
    """One deterministic provider response in an interaction replay."""

    content: str
    tool_calls: tuple[dict[str, Any], ...]
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class CaseManifest:
    """Executable input plus deterministic assertions; never a run record."""

    schema_version: int
    identifier: str
    title: str
    mode: str
    prompt: str
    fixture: str | None
    provider_tape: tuple[ProviderTapeResponse, ...]
    expected_artifacts: dict[str, str]
    expected_delivery_contains: str
    private_values: tuple[str, ...] = ()

    def identity(self) -> dict[str, object]:
        """Stable manifest identity copied into fresh traces."""

        return {
            "id": self.identifier,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "title": self.title,
        }


def load_case_manifest(path: str | Path) -> CaseManifest:
    """Load and validate a JSON case manifest without executing it."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read case manifest {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("case manifest must be a JSON object")

    schema_version = _required_int(raw, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ManifestError(f"unsupported case schema_version {schema_version}; expected {SCHEMA_VERSION}")
    mode = _required_string(raw, "mode")
    if mode not in SUPPORTED_MODES:
        raise ManifestError(f"unsupported case mode {mode!r}")

    tape_raw = raw.get("provider_tape")
    if not isinstance(tape_raw, list) or not tape_raw:
        raise ManifestError("provider_tape must be a non-empty list")
    tape = tuple(_load_tape_response(item, index) for index, item in enumerate(tape_raw, start=1))

    expected_artifacts_raw = raw.get("expected_artifacts", {})
    if not isinstance(expected_artifacts_raw, dict) or not all(
        isinstance(path, str) and isinstance(content, str) for path, content in expected_artifacts_raw.items()
    ):
        raise ManifestError("expected_artifacts must map relative paths to expected UTF-8 content")

    private_values_raw = raw.get("private_values", [])
    if not isinstance(private_values_raw, list) or not all(isinstance(value, str) and value for value in private_values_raw):
        raise ManifestError("private_values must be a list of non-empty strings")

    fixture = raw.get("fixture")
    if fixture is not None and not isinstance(fixture, str):
        raise ManifestError("fixture must be a relative path or null")
    return CaseManifest(
        schema_version=schema_version,
        identifier=_required_string(raw, "id"),
        title=_required_string(raw, "title"),
        mode=mode,
        prompt=_required_string(raw, "prompt"),
        fixture=fixture,
        provider_tape=tape,
        expected_artifacts=dict(expected_artifacts_raw),
        expected_delivery_contains=_required_string(raw, "expected_delivery_contains"),
        private_values=tuple(private_values_raw),
    )


def _load_tape_response(value: object, index: int) -> ProviderTapeResponse:
    if not isinstance(value, dict):
        raise ManifestError(f"provider_tape[{index}] must be an object")
    tool_calls = value.get("tool_calls", [])
    if not isinstance(tool_calls, list) or not all(isinstance(call, dict) for call in tool_calls):
        raise ManifestError(f"provider_tape[{index}].tool_calls must be a list of objects")
    for call in tool_calls:
        if not isinstance(call.get("id"), str) or not isinstance(call.get("name"), str):
            raise ManifestError(f"provider_tape[{index}] tool calls require string id and name")
        if not isinstance(call.get("arguments", {}), (dict, str)):
            raise ManifestError(f"provider_tape[{index}] tool call arguments must be an object or JSON string")
    finish_reason = value.get("finish_reason", "stop")
    if not isinstance(finish_reason, str):
        raise ManifestError(f"provider_tape[{index}].finish_reason must be a string")
    content = value.get("content")
    if not isinstance(content, str):
        raise ManifestError(f"provider_tape[{index}].content must be a string")
    return ProviderTapeResponse(
        content=content,
        tool_calls=tuple(dict(call) for call in tool_calls),
        finish_reason=finish_reason,
    )


def _required_string(value: dict[str, Any], name: str, *, location: str = "case manifest") -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ManifestError(f"{location}.{name} must be a non-empty string")
    return item


def _required_int(value: dict[str, Any], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int):
        raise ManifestError(f"case manifest.{name} must be an integer")
    return item
