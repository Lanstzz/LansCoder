"""Portable, versioned data models used by the evaluation harness."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_MODES = frozenset({"interaction_replay", "fresh_model"})
SUPPORTED_RUNTIME_MODES = frozenset({"l1", "session"})
SUPPORTED_COMPACTION_STRATEGIES = frozenset({"no_compact", "l1_l2", "l1_l2_l3"})
SUPPORTED_PROVIDER_FAULTS = frozenset({"malformed_response", "timeout", "prompt_too_long", "network_error"})
SUPPORTED_TOOL_FAULTS = frozenset({"timeout", "failure", "interrupt"})


class ManifestError(ValueError):
    """A portable case manifest is missing a required field or is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderTapeResponse:
    """One deterministic provider response in an interaction replay."""

    content: str
    tool_calls: tuple[dict[str, Any], ...]
    finish_reason: str = "stop"
    fault: str | None = None


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
    tool_faults: dict[str, str] = field(default_factory=dict)
    tool_result_sizes: dict[str, int] = field(default_factory=dict)
    interrupt_after_tool_calls: int | None = None
    enable_compaction: bool = False
    runtime: str = "l1"
    resume_after_interrupt: bool = False
    resume_prompt: str | None = None
    warmup_prompts: tuple[str, ...] = ()
    context_window: int | None = None
    max_output_tokens: int | None = None
    compaction_strategy: str = "l1_l2_l3"
    capsule_required: bool = False

    def identity(self) -> dict[str, object]:
        """Stable manifest identity copied into fresh traces."""

        return {
            "id": self.identifier,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "title": self.title,
        }


def load_case_manifest(
    path: str | Path,
    *,
    capsule_path: str | Path | None = None,
    capsule_passphrase: str | None = None,
) -> CaseManifest:
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
    runtime = raw.get("runtime", "l1")
    if not isinstance(runtime, str) or runtime not in SUPPORTED_RUNTIME_MODES:
        raise ManifestError(f"runtime must be one of {sorted(SUPPORTED_RUNTIME_MODES)}")

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
    tool_faults_raw = raw.get("tool_faults", {})
    if not isinstance(tool_faults_raw, dict) or not all(
        isinstance(call_id, str) and call_id and isinstance(kind, str) and kind in SUPPORTED_TOOL_FAULTS
        for call_id, kind in tool_faults_raw.items()
    ):
        raise ManifestError("tool_faults must map call IDs to supported fault kinds")
    tool_result_sizes_raw = raw.get("tool_result_sizes", {})
    if not isinstance(tool_result_sizes_raw, dict) or not all(
        isinstance(call_id, str) and call_id and isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= 2_000_000
        for call_id, size in tool_result_sizes_raw.items()
    ):
        raise ManifestError("tool_result_sizes must map call IDs to integer sizes between 0 and 2000000")
    interrupt_after_tool_calls = raw.get("interrupt_after_tool_calls")
    if interrupt_after_tool_calls is not None and (
        isinstance(interrupt_after_tool_calls, bool)
        or not isinstance(interrupt_after_tool_calls, int)
        or interrupt_after_tool_calls < 1
    ):
        raise ManifestError("interrupt_after_tool_calls must be a positive integer or null")
    enable_compaction = raw.get("enable_compaction", False)
    if not isinstance(enable_compaction, bool):
        raise ManifestError("enable_compaction must be a boolean")
    resume_after_interrupt = raw.get("resume_after_interrupt", False)
    if not isinstance(resume_after_interrupt, bool):
        raise ManifestError("resume_after_interrupt must be a boolean")
    resume_prompt = raw.get("resume_prompt")
    if resume_prompt is not None and (not isinstance(resume_prompt, str) or not resume_prompt):
        raise ManifestError("resume_prompt must be a non-empty string or null")
    warmup_prompts_raw = raw.get("warmup_prompts", [])
    if not isinstance(warmup_prompts_raw, list) or not all(isinstance(prompt, str) and prompt for prompt in warmup_prompts_raw):
        raise ManifestError("warmup_prompts must be a list of non-empty strings")
    if len(warmup_prompts_raw) > 64:
        raise ManifestError("warmup_prompts cannot contain more than 64 prompts")
    context_window = raw.get("context_window")
    if context_window is not None and (isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0):
        raise ManifestError("context_window must be a positive integer or null")
    max_output_tokens = raw.get("max_output_tokens")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0
    ):
        raise ManifestError("max_output_tokens must be a positive integer or null")
    compaction_strategy = raw.get("compaction_strategy", "l1_l2_l3")
    if not isinstance(compaction_strategy, str) or compaction_strategy not in SUPPORTED_COMPACTION_STRATEGIES:
        raise ManifestError(f"compaction_strategy must be one of {sorted(SUPPORTED_COMPACTION_STRATEGIES)}")
    if runtime == "l1" and (resume_after_interrupt or warmup_prompts_raw or context_window is not None or max_output_tokens is not None):
        raise ManifestError("session runtime options require runtime='session'")
    if resume_after_interrupt and (interrupt_after_tool_calls is None or resume_prompt is None):
        raise ManifestError("resume_after_interrupt requires interrupt_after_tool_calls and resume_prompt")
    capsule = raw.get("capsule")
    capsule_required = isinstance(capsule, dict)
    if capsule is not None:
        if capsule_path is None and capsule_passphrase is None:
            # A portable manifest is useful for review and transport on its
            # own.  The replay runner must additionally hydrate it before
            # execution; keep parsing available for cataloguing and audits.
            capsule = None
        elif capsule_path is None or capsule_passphrase is None:
            raise ManifestError("capsule_path and capsule_passphrase must be provided together")
    if capsule_path is not None and capsule_passphrase is not None:
        from eval_harness.trace.capsule import CapsuleError, capsule_payload_digest, read_capsule

        try:
            material = read_capsule(capsule_path, capsule_passphrase)
        except CapsuleError as exc:
            raise ManifestError(f"cannot load replay capsule: {exc}") from exc
        expected_capsule = raw.get("capsule")
        expected_digest = expected_capsule.get("material_sha256") if isinstance(expected_capsule, dict) else None
        if not isinstance(expected_digest, str) or capsule_payload_digest(material) != expected_digest:
            raise ManifestError("replay capsule does not match the case manifest")
        raw = {
            **raw,
            "prompt": material.get("prompt"),
            "provider_tape": material.get("provider_tape"),
            "expected_delivery_contains": material.get("expected_delivery_contains"),
            "private_values": material.get("private_values", []),
            "tool_faults": material.get("tool_faults", raw.get("tool_faults", {})),
        }
        tape_raw = raw.get("provider_tape")
        if not isinstance(tape_raw, list) or not tape_raw:
            raise ManifestError("replay capsule provider_tape must be a non-empty list")
        tape = tuple(_load_tape_response(item, index) for index, item in enumerate(tape_raw, start=1))
        private_values_raw = raw.get("private_values", [])
        if not isinstance(private_values_raw, list) or not all(isinstance(value, str) and value for value in private_values_raw):
            raise ManifestError("replay capsule private_values must be a list of non-empty strings")
        tool_faults_raw = raw.get("tool_faults", {})
        if not isinstance(tool_faults_raw, dict) or not all(
            isinstance(call_id, str) and call_id and isinstance(kind, str) and kind in SUPPORTED_TOOL_FAULTS
            for call_id, kind in tool_faults_raw.items()
        ):
            raise ManifestError("replay capsule tool_faults must map call IDs to supported fault kinds")
        capsule_required = False
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
        tool_faults=dict(tool_faults_raw),
        tool_result_sizes=dict(tool_result_sizes_raw),
        interrupt_after_tool_calls=interrupt_after_tool_calls,
        enable_compaction=enable_compaction,
        runtime=runtime,
        resume_after_interrupt=resume_after_interrupt,
        resume_prompt=resume_prompt,
        warmup_prompts=tuple(warmup_prompts_raw),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        compaction_strategy=compaction_strategy,
        capsule_required=capsule_required,
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
    content = value.get("content", "")
    if not isinstance(content, str):
        raise ManifestError(f"provider_tape[{index}].content must be a string")
    fault = value.get("fault")
    if fault is not None and (not isinstance(fault, str) or fault not in SUPPORTED_PROVIDER_FAULTS):
        raise ManifestError(f"provider_tape[{index}].fault must be one of {sorted(SUPPORTED_PROVIDER_FAULTS)}")
    return ProviderTapeResponse(
        content=content,
        tool_calls=tuple(dict(call) for call in tool_calls),
        finish_reason=finish_reason,
        fault=fault,
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
