"""Extract portable replay cases from LansCoder or Harbor history."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval_harness.schema.models import SCHEMA_VERSION, CaseManifest, load_case_manifest
from eval_harness.trace.capsule import capsule_payload_digest, write_capsule


class ExtractionError(ValueError):
    """A history source cannot be converted into an executable replay case."""


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Paths and metadata produced by one history extraction."""

    case_path: Path
    capsule_path: Path
    case: CaseManifest
    source_kind: str
    material_sha256: str


def extract_replay_case(
    source: str | Path,
    output: str | Path,
    *,
    capsule: str | Path,
    passphrase: str,
    repository_root: str | Path | None = None,
    sensitive_values: tuple[str, ...] = (),
) -> ExtractionResult:
    """Extract history into a redacted manifest and an encrypted local capsule.

    ``output`` is the portable JSON manifest.  ``capsule`` must be supplied
    explicitly because recoverable replay material is never written beside the
    manifest implicitly or embedded in it.
    """

    source_path = Path(source)
    event_source, source_kind, source_name = _load_source(source_path)
    material = _build_material(event_source, source_kind, source_name, sensitive_values=sensitive_values)
    material_digest = capsule_payload_digest(material)
    capsule_path = Path(capsule)
    write_capsule(capsule_path, material, passphrase, repository_root=repository_root or Path.cwd())

    manifest_dict = _portable_manifest(material, material_digest)
    output_path = Path(output)
    if output_path.exists():
        raise FileExistsError(f"case manifest already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest_dict, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = load_case_manifest(output_path)
    return ExtractionResult(
        case_path=output_path,
        capsule_path=capsule_path,
        case=manifest,
        source_kind=source_kind,
        material_sha256=material_digest,
    )


def extract_case(*args: Any, **kwargs: Any) -> ExtractionResult:
    """Short alias for :func:`extract_replay_case`."""

    return extract_replay_case(*args, **kwargs)


def _load_source(source: Path) -> tuple[list[dict[str, Any]], str, str]:
    if source.is_dir():
        candidates = [
            source / "lanscoder-session.jsonl",
            source / "trace.jsonl",
            *sorted(source.rglob("*session*.jsonl")),
            *sorted(source.rglob("*trace*.jsonl")),
        ]
        selected = next((candidate for candidate in candidates if candidate.is_file()), None)
        if selected is None:
            raise ExtractionError(f"no LansCoder session or eval trace found below {source}")
        source = selected
    if not source.is_file():
        raise ExtractionError(f"history source does not exist: {source}")
    try:
        if source.suffix.lower() == ".jsonl":
            events = _read_jsonl(source)
        else:
            value = json.loads(source.read_text(encoding="utf-8"))
            events = _find_event_list(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"cannot read history source {source}: {exc}") from exc
    if not events:
        raise ExtractionError(f"history source contains no structured events: {source}")
    source_kind = "trace" if any("data" in event and "schema_version" in event for event in events) else "session"
    return events, source_kind, source.name


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"invalid JSONL event at line {line_number}: {exc}") from exc
        if isinstance(value, dict):
            events.append(value)
    return events


def _find_event_list(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = [item for item in value if isinstance(item, dict)]
        if items and any("type" in item for item in items):
            return items
        for item in value:
            found = _find_event_list(item)
            if found:
                return found
    elif isinstance(value, dict):
        if isinstance(value.get("events"), list):
            found = _find_event_list(value["events"])
            if found:
                return found
        for item in value.values():
            found = _find_event_list(item)
            if found:
                return found
    return []


def _build_material(
    events: list[dict[str, Any]],
    source_kind: str,
    source_name: str,
    *,
    sensitive_values: tuple[str, ...],
) -> dict[str, Any]:
    if source_kind == "trace":
        return _build_trace_material(events, source_name, sensitive_values=sensitive_values)
    return _build_session_material(events, source_name, sensitive_values=sensitive_values)


def _build_trace_material(events: list[dict[str, Any]], source_name: str, *, sensitive_values: tuple[str, ...]) -> dict[str, Any]:
    prompt = ""
    provider_tape: list[dict[str, Any]] = []
    final_delivery = ""
    for event in events:
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "user_input" and isinstance(data.get("content"), str) and not prompt:
            prompt = data["content"]
        elif event_type == "provider_response":
            provider_tape.append(
                {
                    "content": str(data.get("content") or ""),
                    "tool_calls": _trace_tool_calls(data.get("tool_calls")),
                    "finish_reason": str(data.get("finish_reason") or "stop"),
                }
            )
        elif event_type == "provider_error":
            fault = _provider_fault(data.get("kind"))
            if fault is not None:
                provider_tape.append({"content": "", "tool_calls": [], "finish_reason": "error", "fault": fault})
        elif event_type == "final_delivery" and isinstance(data.get("content"), str):
            final_delivery = data["content"]
    if not prompt:
        raise ExtractionError(f"{source_name} has no user_input event")
    if not provider_tape:
        raise ExtractionError(f"{source_name} has no provider response or error event")
    return _material_payload(prompt, provider_tape, final_delivery, source_name, sensitive_values=sensitive_values)


def _build_session_material(events: list[dict[str, Any]], source_name: str, *, sensitive_values: tuple[str, ...]) -> dict[str, Any]:
    prompt = ""
    provider_tape: list[dict[str, Any]] = []
    final_delivery = ""
    tool_faults: dict[str, str] = {}
    title = ""
    for event in events:
        event_type = str(event.get("type") or "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event_type == "session_created":
            title = str(payload.get("title") or "")
        elif event_type == "user_message" and not prompt:
            prompt = _parts_text(payload, "text")
        elif event_type == "assistant_message":
            calls = _session_tool_calls(payload)
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            provider_tape.append(
                {
                    "content": _parts_text(payload, "text"),
                    "tool_calls": calls,
                    "finish_reason": str(metadata.get("finish_reason") or ("tool_calls" if calls else "stop")),
                }
            )
            if _parts_text(payload, "text"):
                final_delivery = _parts_text(payload, "text")
        elif event_type == "tool_result":
            for part in payload.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                metadata = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
                fault = metadata.get("fault")
                metadata_data = metadata.get("data")
                if fault is None and isinstance(metadata_data, dict):
                    fault = metadata_data.get("fault")
                call_id = metadata.get("tool_call_id")
                if isinstance(call_id, str) and fault in {"timeout", "failure", "interrupt"}:
                    tool_faults[call_id] = fault
    if not prompt:
        raise ExtractionError(f"{source_name} has no user_message event")
    if not provider_tape:
        raise ExtractionError(f"{source_name} has no assistant_message event")
    material = _material_payload(prompt, provider_tape, final_delivery, source_name, sensitive_values=sensitive_values)
    material["tool_faults"] = tool_faults
    material["title"] = title
    return material


def _material_payload(
    prompt: str,
    provider_tape: list[dict[str, Any]],
    final_delivery: str,
    source_name: str,
    *,
    sensitive_values: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_name": source_name,
        "prompt": prompt,
        "provider_tape": provider_tape,
        "expected_delivery_contains": final_delivery or _last_content(provider_tape),
        "private_values": list(sensitive_values),
    }


def _portable_manifest(material: dict[str, Any], material_digest: str) -> dict[str, Any]:
    tape = []
    for response in material["provider_tape"]:
        tool_calls = []
        for call in response.get("tool_calls", []):
            arguments = call.get("arguments", {})
            tool_calls.append(
                {
                    "id": str(call.get("id") or "call"),
                    "name": str(call.get("name") or "tool"),
                    "arguments": {
                        "__capsule__": "tool_arguments",
                        "sha256": _digest_json(arguments),
                    },
                }
            )
        item = {
            "content": _placeholder("provider_response", response.get("content") or ""),
            "tool_calls": tool_calls,
            "finish_reason": str(response.get("finish_reason") or "stop"),
        }
        if response.get("fault") is not None:
            item["fault"] = response["fault"]
        tape.append(item)
    identifier = _case_id(str(material["source_name"]), material_digest)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": identifier,
        "title": f"Extracted replay ({identifier})",
        "mode": "interaction_replay",
        "prompt": _placeholder("user_input", material["prompt"]),
        "fixture": None,
        "provider_tape": tape,
        "expected_artifacts": {},
        "expected_delivery_contains": _placeholder("delivery", material["expected_delivery_contains"]),
        "private_values": [],
        "tool_faults": material.get("tool_faults", {}),
        "capsule": {"format": "lanscoder-eval-capsule", "version": 1, "material_sha256": material_digest},
    }


def _parts_text(payload: dict[str, Any], kind: str) -> str:
    return "\n".join(
        str(part.get("content") or "")
        for part in payload.get("parts") or []
        if isinstance(part, dict) and part.get("kind") == kind
    )


def _session_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    calls = []
    for part in payload.get("parts") or []:
        if not isinstance(part, dict) or part.get("kind") != "tool_call":
            continue
        metadata = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
        calls.append(
            {
                "id": str(metadata.get("tool_call_id") or part.get("id") or "call"),
                "name": str(metadata.get("tool_name") or "tool"),
                "arguments": metadata.get("arguments") if isinstance(metadata.get("arguments"), (dict, str)) else {},
            }
        )
    return calls


def _trace_tool_calls(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "id": str(item.get("id") or "call"),
                "name": str(item.get("name") or "tool"),
                "arguments": {},
            }
        )
    return result


def _provider_fault(value: object) -> str | None:
    return {
        "api_error": "malformed_response",
        "malformed_response": "malformed_response",
        "timeout": "timeout",
        "prompt_too_long": "prompt_too_long",
        "network_error": "network_error",
    }.get(str(value))


def _last_content(tape: list[dict[str, Any]]) -> str:
    for response in reversed(tape):
        if response.get("content"):
            return str(response["content"])
    return "replay completed"


def _placeholder(kind: str, value: object) -> str:
    return f"<CAPSULE:{kind}:{_digest_json(value)}>"


def _digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _case_id(source_name: str, material_digest: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(source_name).stem).strip("-") or "history"
    return f"history-{stem[:32]}-{material_digest[:12]}"
