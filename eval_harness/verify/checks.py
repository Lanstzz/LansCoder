"""Deterministic verification and machine-readable scorecard construction."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Any

from eval_harness.schema.models import CaseManifest, SCHEMA_VERSION
from eval_harness.trace.canonicalize import load_trace
from eval_harness.trace.recorder import trace_digest


def verify_run(manifest: CaseManifest, trace_path: str | Path, artifacts_path: str | Path) -> dict[str, dict[str, object]]:
    """Evaluate the five hard gates without reading hidden verifier inputs."""

    trace = load_trace(trace_path)
    return {
        "trace": _trace_gate(manifest, trace),
        "artifact": _artifact_gate(manifest, Path(artifacts_path), trace),
        "recovery": _recovery_gate(trace),
        "security": _security_gate(manifest, Path(trace_path)),
        "delivery": _delivery_gate(manifest, trace),
    }


def build_scorecard(
    verification: dict[str, dict[str, object]],
    trace_path: str | Path,
    *,
    comparison: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create a portable scorecard that separates hard gates from metrics."""

    trace = load_trace(trace_path)
    type_counts = Counter(str(event.get("type")) for event in trace)
    completed = next((event.get("data", {}) for event in reversed(trace) if event.get("type") == "run_completed"), {})
    if not isinstance(completed, dict):
        completed = {}
    scorecard: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "passed": all(bool(gate.get("passed")) for gate in verification.values()),
        "gates": verification,
        "metrics": {
            "provider_calls": type_counts["provider_request"],
            "tool_calls": type_counts["tool_execution_start"],
            "provider_errors": type_counts["provider_error"],
            "tool_errors": sum(
                1
                for event in trace
                if event.get("type") == "tool_execution_end"
                and isinstance(event.get("data"), dict)
                and event["data"].get("is_error")
            ),
            "elapsed_ms": completed.get("elapsed_ms"),
            "context_compactions": type_counts["context_compaction"] + type_counts["context_compaction_l3"],
            "successful_compactions": sum(
                1
                for event in trace
                if event.get("type") in {"context_compaction", "context_compaction_l3"}
                and isinstance(event.get("data"), dict)
                and event["data"].get("status") == "success"
            ),
            "session_resumes": type_counts["session_resumed"],
            "recovery_events": len(completed.get("recovery_events", [])) if isinstance(completed.get("recovery_events", []), list) else 0,
            "token_usage": None,
        },
    }
    if comparison is not None:
        scorecard["comparison"] = comparison
    return scorecard


def compare_scorecards(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, object]:
    """Compare gate regressions and numeric metrics without a language-model judge."""

    baseline_gates = baseline.get("gates", {})
    current_gates = current.get("gates", {})
    regressions = sorted(
        gate
        for gate, baseline_result in baseline_gates.items()
        if isinstance(baseline_result, dict)
        and baseline_result.get("passed")
        and (not isinstance(current_gates.get(gate), dict) or not current_gates[gate].get("passed"))
    )
    baseline_metrics = baseline.get("metrics", {})
    current_metrics = current.get("metrics", {})
    deltas = {
        key: current_metrics[key] - baseline_metrics[key]
        for key in set(baseline_metrics) & set(current_metrics)
        if isinstance(baseline_metrics[key], (int, float))
        and not isinstance(baseline_metrics[key], bool)
        and isinstance(current_metrics[key], (int, float))
        and not isinstance(current_metrics[key], bool)
    }
    return {
        "passed": not regressions,
        "baseline_passed": bool(baseline.get("passed")),
        "current_passed": bool(current.get("passed")),
        "gate_regressions": regressions,
        "metric_deltas": deltas,
    }


def _trace_gate(manifest: CaseManifest, trace: list[dict[str, Any]]) -> dict[str, object]:
    errors: list[str] = []
    if not trace:
        return _failed("trace is empty")
    for expected_sequence, event in enumerate(trace, start=1):
        if not isinstance(event.get("schema_version"), int) or isinstance(event.get("schema_version"), bool):
            errors.append(f"sequence {expected_sequence} has an invalid schema version")
        elif event.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"sequence {expected_sequence} has an unsupported schema version")
        if not isinstance(event.get("sequence"), int) or isinstance(event.get("sequence"), bool):
            errors.append(f"sequence {expected_sequence} has an invalid sequence number")
        elif event.get("sequence") != expected_sequence:
            errors.append(f"sequence is not contiguous at event {expected_sequence}")
        if not isinstance(event.get("type"), str) or not event.get("type"):
            errors.append(f"sequence {expected_sequence} has an invalid event type")
        if not isinstance(event.get("data"), dict):
            errors.append(f"sequence {expected_sequence} has non-object event data")
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str):
            errors.append(f"sequence {expected_sequence} has an invalid timestamp")
        else:
            try:
                datetime.fromisoformat(timestamp)
            except ValueError:
                errors.append(f"sequence {expected_sequence} has an invalid timestamp")

    event_types = [str(event.get("type")) for event in trace]
    if event_types[0] != "run_started":
        errors.append("run_started must be the first event")
    required = {"run_started", "user_input", "provider_request", "artifacts", "final_delivery", "run_completed", "trace_integrity"}
    actual = set(event_types)
    missing = sorted(required - actual)
    if missing:
        errors.append(f"missing required events: {', '.join(missing)}")

    for event_type in ("run_started", "user_input", "artifacts", "final_delivery", "run_completed", "trace_integrity"):
        if event_types.count(event_type) != 1:
            errors.append(f"trace must contain exactly one {event_type} event")
    if event_types.count("network_guard") != 1:
        errors.append("trace must contain exactly one network_guard event")

    started = _single_event(trace, "run_started")
    started_data = started.get("data", {}) if started is not None else {}
    if isinstance(started_data, dict):
        _validate_run_identity(manifest, started_data, errors)

    user_input = _single_event(trace, "user_input")
    if user_input is not None:
        data = user_input.get("data", {})
        if not isinstance(data, dict) or not isinstance(data.get("content"), str) or not data.get("content"):
            errors.append("user_input must contain non-empty content")
        elif not _is_sha256(data.get("sha256")):
            errors.append("user_input must contain a SHA-256 fingerprint")
        elif data.get("sha256") != _sha256(manifest.prompt):
            errors.append("user_input fingerprint does not match the case prompt")

    announced_calls = _validate_provider_interactions(trace, errors)
    _validate_tool_lifecycles(trace, announced_calls, errors)
    _validate_completion(manifest, trace, errors)

    integrity = _single_event(trace, "trace_integrity")
    if trace[-1].get("type") != "trace_integrity":
        errors.append("trace_integrity must be the final event")
    if integrity is not None:
        data = integrity.get("data", {})
        if not isinstance(data, dict) or data.get("algorithm") != "sha256":
            errors.append("trace integrity must declare sha256")
        elif not _is_sha256(data.get("digest")):
            errors.append("trace integrity must contain a SHA-256 digest")
        elif data.get("digest") != trace_digest(trace[:-1]):
            errors.append("trace integrity digest does not match the preceding events")
        if not isinstance(data, dict) or data.get("event_count") != len(trace) - 1:
            errors.append("trace integrity event_count does not match the trace prefix")
    return _result(errors)


def _validate_run_identity(manifest: CaseManifest, data: dict[str, Any], errors: list[str]) -> None:
    expected_case = manifest.identity()
    case = data.get("case")
    if not isinstance(case, dict):
        errors.append("run_started is missing case identity")
    else:
        for field, expected in expected_case.items():
            if case.get(field) != expected:
                errors.append(f"case identity field differs: {field}")

    expected_runtime = {
        "api": "lanscoder.core.create_agent_session" if manifest.runtime == "session" else "lanscoder.core.agent_loop",
        "mode": manifest.runtime,
        "session_id": f"eval-{manifest.identifier}",
    }
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("run_started is missing runtime identity")
    else:
        for field, expected in expected_runtime.items():
            if runtime.get(field) != expected:
                errors.append(f"runtime identity field differs: {field}")

    expected_config = {
        "provider": "scripted",
        "model": "offline-tape-v1",
        "streaming": False,
        "context_window": manifest.context_window,
        "max_output_tokens": manifest.max_output_tokens,
        "compaction_strategy": manifest.compaction_strategy,
    }
    config = data.get("config")
    if not isinstance(config, dict):
        errors.append("run_started is missing config identity")
    else:
        for field, expected in expected_config.items():
            if config.get(field) != expected:
                errors.append(f"config identity field differs: {field}")
    if data.get("network_policy") != "disabled":
        errors.append("run_started must declare the disabled network policy")


def _validate_provider_interactions(
    trace: list[dict[str, Any]], errors: list[str]
) -> dict[str, list[tuple[str, str]]]:
    announced: dict[str, list[tuple[str, str]]] = {}
    request_open = False
    request_count = 0
    response_count = 0
    for event in trace:
        event_type = event.get("type")
        data = event.get("data")
        if event_type == "provider_request":
            request_count += 1
            if request_open:
                errors.append("provider request is missing a response or error")
            request_open = True
            if not isinstance(data, dict):
                errors.append("provider_request data must be an object")
                continue
            _validate_provider_request(data, errors)
        elif event_type in {"provider_response", "provider_error"}:
            response_count += 1
            if not request_open:
                errors.append(f"{event_type} has no preceding provider request")
            request_open = False
            if not isinstance(data, dict):
                errors.append(f"{event_type} data must be an object")
                continue
            if event_type == "provider_response":
                calls = data.get("tool_calls")
                if not isinstance(calls, list):
                    errors.append("provider_response tool_calls must be a list")
                    continue
                _validate_provider_response(data, calls, announced, errors)
            else:
                _validate_provider_error(data, errors)
    if request_open:
        errors.append("provider request is missing a response or error")
    if request_count != response_count:
        errors.append(f"provider interactions are unbalanced: {request_count} requests, {response_count} outcomes")
    return announced


def _validate_provider_request(data: dict[str, Any], errors: list[str]) -> None:
    messages = data.get("messages")
    if not isinstance(messages, list):
        errors.append("provider_request messages must be a list")
    else:
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("role"), str):
                errors.append("provider_request messages must contain role objects")
                continue
            role = message["role"]
            content = message.get("content")
            if not isinstance(content, str):
                errors.append("provider_request message content must be a string")
            if role == "system":
                if content != "<SYSTEM_PROMPT_REDACTED>" or not _is_sha256(message.get("content_sha256")):
                    errors.append("system prompt must be redacted and fingerprinted")
            if role == "tool":
                if content != "<TOOL_RESULT_REDACTED>" or not _is_sha256(message.get("content_sha256")):
                    errors.append("tool result must be redacted and fingerprinted")
    tools = data.get("tools")
    if not isinstance(tools, list):
        errors.append("provider_request tools must be a list")
    else:
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                errors.append("provider_request tools must contain named objects")
                continue
            if tool.get("description") != "<TOOL_DESCRIPTION_REDACTED>" or not _is_sha256(tool.get("description_sha256")):
                errors.append(f"tool description is not redacted: {tool.get('name')}")
            if tool.get("parameters") != "<TOOL_PARAMETERS_REDACTED>" or not _is_sha256(tool.get("parameters_sha256")):
                errors.append(f"tool parameters are not redacted: {tool.get('name')}")
            if not isinstance(tool.get("parameter_keys"), list):
                errors.append(f"tool parameter summary is missing: {tool.get('name')}")


def _validate_provider_response(
    data: dict[str, Any],
    calls: list[Any],
    announced: dict[str, list[tuple[str, str]]],
    errors: list[str],
) -> None:
    for field in ("provider", "model", "finish_reason"):
        if not isinstance(data.get(field), str) or not data.get(field):
            errors.append(f"provider_response is missing {field}")
    if not isinstance(data.get("content"), str):
        errors.append("provider_response content must be a string")
    for call in calls:
        if not isinstance(call, dict):
            errors.append("provider_response tool_calls must contain objects")
            continue
        call_id, name, fingerprint = call.get("id"), call.get("name"), call.get("arguments_sha256")
        if not isinstance(call_id, str) or not call_id:
            errors.append("provider tool call is missing an id")
            continue
        if not isinstance(name, str) or not name:
            errors.append(f"provider tool call is missing a name: {call_id}")
        if not _is_sha256(fingerprint):
            errors.append(f"provider tool call has an invalid argument fingerprint: {call_id}")
        if not isinstance(call.get("argument_keys"), list):
            errors.append(f"provider tool call is missing argument keys: {call_id}")
        announced.setdefault(call_id, []).append((str(name), str(fingerprint)))


def _validate_provider_error(data: dict[str, Any], errors: list[str]) -> None:
    if not isinstance(data.get("kind"), str) or not data.get("kind"):
        errors.append("provider_error is missing kind")
    if not isinstance(data.get("provider_error_kind"), str) or not data.get("provider_error_kind"):
        errors.append("provider_error is missing provider_error_kind")
    if not isinstance(data.get("message"), str):
        errors.append("provider_error message must be a string")
    if not isinstance(data.get("recoverable"), bool):
        errors.append("provider_error recoverable must be boolean")


def _validate_tool_lifecycles(
    trace: list[dict[str, Any]], announced: dict[str, list[tuple[str, str]]], errors: list[str]
) -> None:
    states: dict[str, str] = {}
    consumed: Counter[str] = Counter()
    for event in trace:
        event_type = event.get("type")
        if event_type not in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            errors.append(f"{event_type} data must be an object")
            continue
        call_id, tool_name = data.get("tool_call_id"), data.get("tool_name")
        if not isinstance(call_id, str) or not call_id:
            errors.append(f"{event_type} is missing tool_call_id")
            continue
        if not isinstance(tool_name, str) or not tool_name:
            errors.append(f"{event_type} is missing tool_name: {call_id}")
        if event_type == "tool_execution_start":
            fingerprint = data.get("arguments_sha256")
            if not _is_sha256(fingerprint):
                errors.append(f"tool start has an invalid argument fingerprint: {call_id}")
            if not isinstance(data.get("argument_keys"), list):
                errors.append(f"tool start is missing argument keys: {call_id}")
            expected_calls = announced.get(call_id, [])
            index = consumed[call_id]
            if index >= len(expected_calls):
                errors.append(f"tool start was not announced by provider: {call_id}")
            else:
                expected_name, expected_fingerprint = expected_calls[index]
                if tool_name != expected_name or fingerprint != expected_fingerprint:
                    errors.append(f"tool start differs from provider call: {call_id}")
                consumed[call_id] += 1
            if call_id in states:
                errors.append(f"duplicate tool start: {call_id}")
            else:
                states[call_id] = "started"
        elif event_type == "tool_execution_update":
            if call_id not in states:
                errors.append(f"tool update has no start: {call_id}")
            elif states[call_id] not in {"started", "ended_error", "ended"}:
                errors.append(f"tool update occurs after terminal state: {call_id}")
            lifecycle = data.get("lifecycle")
            if not isinstance(lifecycle, str) or not lifecycle:
                errors.append(f"tool update is missing lifecycle: {call_id}")
            elif lifecycle == "interrupted" and call_id in states:
                states[call_id] = "interrupted"
        else:
            if call_id not in states:
                errors.append(f"tool end has no start: {call_id}")
            elif states[call_id] != "started":
                errors.append(f"tool end occurs after terminal state: {call_id}")
            _validate_tool_result(data, call_id, errors)
            if call_id in states and states[call_id] == "started":
                states[call_id] = "ended"
    for call_id, state in sorted(states.items()):
        if state == "started":
            errors.append(f"unsettled tool lifecycle: {call_id}")
    for call_id, expected_calls in sorted(announced.items()):
        if consumed[call_id] != len(expected_calls):
            errors.append(f"provider tool call has no execution start: {call_id}")


def _validate_tool_result(data: dict[str, Any], call_id: str, errors: list[str]) -> None:
    if not isinstance(data.get("is_error"), bool):
        errors.append(f"tool end is missing boolean is_error: {call_id}")
    result = data.get("result")
    if not isinstance(result, dict):
        errors.append(f"tool end is missing result: {call_id}")
        return
    for field in ("name", "ok", "content_sha256", "has_error", "data_keys", "fault", "timed_out"):
        if field not in result:
            errors.append(f"tool result is missing {field}: {call_id}")
    if not isinstance(result.get("name"), str) or not isinstance(result.get("ok"), bool):
        errors.append(f"tool result identity is invalid: {call_id}")
    if not _is_sha256(result.get("content_sha256")):
        errors.append(f"tool result has an invalid content fingerprint: {call_id}")
    if isinstance(result.get("ok"), bool) and isinstance(data.get("is_error"), bool) and data["is_error"] == result["ok"]:
        errors.append(f"tool error status disagrees with result: {call_id}")
    if not isinstance(result.get("has_error"), bool) or not isinstance(result.get("data_keys"), list):
        errors.append(f"tool result summary is invalid: {call_id}")
    if not isinstance(result.get("timed_out"), bool):
        errors.append(f"tool timeout status is invalid: {call_id}")


def _validate_completion(manifest: CaseManifest, trace: list[dict[str, Any]], errors: list[str]) -> None:
    artifacts = _single_event(trace, "artifacts")
    if artifacts is not None and not isinstance(artifacts.get("data"), dict):
        errors.append("artifacts data must be an object")
    network = _single_event(trace, "network_guard")
    if network is not None:
        data = network.get("data", {})
        if not isinstance(data, dict) or not isinstance(data.get("attempts"), int) or data.get("attempts", -1) < 0:
            errors.append("network_guard attempts must be a non-negative integer")

    delivery = _single_event(trace, "final_delivery")
    completed = _single_event(trace, "run_completed")
    if delivery is None or completed is None:
        return
    delivery_data = delivery.get("data", {})
    completed_data = completed.get("data", {})
    if not isinstance(delivery_data, dict):
        errors.append("final_delivery data must be an object")
        return
    if not isinstance(delivery_data.get("content"), str) or not isinstance(delivery_data.get("completed"), bool):
        errors.append("final_delivery must contain string content and boolean completed")
    if not isinstance(completed_data, dict):
        errors.append("run_completed data must be an object")
        return
    for field in ("provider_calls", "tool_calls", "compaction_events"):
        value = completed_data.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"run_completed {field} must be a non-negative integer")
    if completed_data.get("provider_calls") != sum(event.get("type") == "provider_request" for event in trace):
        errors.append("run_completed provider_calls disagrees with trace")
    if completed_data.get("tool_calls") != sum(event.get("type") == "tool_execution_start" for event in trace):
        errors.append("run_completed tool_calls disagrees with trace")
    if completed_data.get("compaction_events") != sum(
        event.get("type") in {"context_compaction", "context_compaction_l3"} for event in trace
    ):
        errors.append("run_completed compaction_events disagrees with trace")
    elapsed = completed_data.get("elapsed_ms")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        errors.append("run_completed elapsed_ms must be non-negative")
    failure = completed_data.get("failure")
    if failure is not None and not isinstance(failure, str):
        errors.append("run_completed failure must be a string or null")
    has_exception = any(event.get("type") == "runtime_exception" for event in trace)
    if (failure is None) == has_exception:
        errors.append("run_completed failure does not match runtime_exception evidence")
    if delivery_data.get("completed") is not (failure is None):
        errors.append("final_delivery completion does not match run_completed failure")
    session_resumes = sum(event.get("type") == "session_resumed" for event in trace)
    if manifest.resume_after_interrupt and session_resumes != 1:
        errors.append("resume_after_interrupt requires exactly one session_resumed event")
    if not manifest.resume_after_interrupt and session_resumes:
        errors.append("session_resumed event is not allowed without resume_after_interrupt")
    if session_resumes and manifest.runtime != "session":
        errors.append("session_resumed event requires session runtime")
    assistant_deliveries = [
        event["data"]["message"].get("content")
        for event in trace
        if event.get("type") == "message_end"
        and isinstance(event.get("data"), dict)
        and isinstance(event["data"].get("message"), dict)
        and event["data"]["message"].get("role") == "assistant"
        and isinstance(event["data"]["message"].get("content"), str)
    ]
    if assistant_deliveries and delivery_data.get("content") != assistant_deliveries[-1]:
        errors.append("final_delivery does not match the terminal assistant message")
    recovery_events = completed_data.get("recovery_events")
    if not isinstance(recovery_events, list):
        errors.append("run_completed recovery_events must be a list")
    elif recovery_events != _recovery_summary(trace):
        errors.append("run_completed recovery_events disagree with trace evidence")
def _recovery_summary(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in trace:
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "provider_error":
            summary.append({"kind": "provider", "error": data.get("kind"), "provider_error_kind": data.get("provider_error_kind")})
        elif event_type == "tool_execution_end" and data.get("is_error"):
            result = data.get("result")
            summary.append({"kind": "tool", "tool_name": data.get("tool_name"), "fault": result.get("fault") if isinstance(result, dict) else None})
        elif event_type == "tool_execution_update" and data.get("lifecycle") == "interrupted":
            summary.append({"kind": "interrupt", "tool_call_id": data.get("tool_call_id")})
        elif event_type == "context_compaction":
            summary.append({"kind": "compaction", "status": data.get("status")})
        elif event_type == "context_compaction_l3":
            summary.append({"kind": "compaction_l3", "status": data.get("status")})
        elif event_type == "session_resumed":
            summary.append({"kind": "session_resume", "interrupted": data.get("interrupted")})
    return summary


def _single_event(trace: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    matches = [event for event in trace if event.get("type") == event_type]
    return matches[0] if len(matches) == 1 else None


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_gate(manifest: CaseManifest, artifacts_path: Path, trace: list[dict[str, Any]]) -> dict[str, object]:
    errors: list[str] = []
    for relative_path, expected_content in manifest.expected_artifacts.items():
        target = artifacts_path / relative_path
        if not target.is_file():
            errors.append(f"required artifact is missing: {relative_path}")
        elif target.read_text(encoding="utf-8") != expected_content:
            errors.append(f"artifact content differs: {relative_path}")
    artifact_event = next((event.get("data") for event in reversed(trace) if event.get("type") == "artifacts"), {})
    if not isinstance(artifact_event, dict):
        errors.append("artifact diff event is missing")
    else:
        for field in ("created", "modified", "deleted", "files"):
            if field not in artifact_event:
                errors.append(f"artifact diff is missing {field}")
        for path in artifact_event.get("files", {}) if isinstance(artifact_event.get("files"), dict) else {}:
            if Path(str(path)).is_absolute() or ".." in Path(str(path)).parts:
                errors.append(f"artifact diff contains an unsafe path: {path}")
        for field, expected_paths in manifest.expected_artifact_diff.items():
            actual_paths = artifact_event.get(field)
            if not isinstance(actual_paths, list):
                errors.append(f"artifact diff field is not a list: {field}")
            elif sorted(str(path) for path in actual_paths) != sorted(expected_paths):
                errors.append(f"artifact diff {field} differs: expected {sorted(expected_paths)}, got {sorted(actual_paths)}")
        changed_paths = {
            str(path)
            for field in ("created", "modified", "deleted")
            for path in artifact_event.get(field, [])
            if isinstance(artifact_event.get(field), list)
        }
        for forbidden_path in manifest.forbidden_paths:
            if forbidden_path in changed_paths or (artifacts_path / forbidden_path).exists():
                errors.append(f"forbidden artifact path was touched: {forbidden_path}")
    return _result(errors)


def _recovery_gate(trace: list[dict[str, Any]]) -> dict[str, object]:
    states: dict[str, str] = {}
    errors: list[str] = []
    provider_errors: list[str] = []
    tool_errors: list[str] = []
    interruptions: list[str] = []
    duplicate_tool_ids: list[str] = []
    compactions = 0
    for event in trace:
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        call_id = data.get("tool_call_id")
        if isinstance(call_id, str):
            if event.get("type") == "tool_execution_start":
                if call_id in states:
                    errors.append(f"duplicate tool start: {call_id}")
                    duplicate_tool_ids.append(call_id)
                states[call_id] = "started"
            elif event.get("type") == "tool_execution_end":
                if call_id not in states:
                    errors.append(f"tool ended without a start: {call_id}")
                elif states[call_id] != "started":
                    errors.append(f"tool ended after terminal state: {call_id}")
                result = data.get("result")
                if isinstance(result, dict) and result.get("fault"):
                    tool_errors.append(str(result["fault"]))
                states[call_id] = "ended_error" if data.get("is_error") else "ended"
            elif event.get("type") == "tool_execution_update" and data.get("lifecycle") == "interrupted":
                if call_id not in states:
                    errors.append(f"interrupted tool has no start: {call_id}")
                elif states[call_id] not in {"started", "ended_error", "ended"}:
                    errors.append(f"tool interrupted after terminal state: {call_id}")
                states[call_id] = "interrupted"
                interruptions.append(call_id)
        if event.get("type") == "provider_error":
            provider_errors.append(str(data.get("kind", "unknown")))
        if event.get("type") in {"context_compaction", "context_compaction_l3"}:
            compactions += 1
            if event.get("type") == "context_compaction" and (
                not isinstance(data.get("before_tokens"), int) or not isinstance(data.get("after_tokens"), int)
            ):
                errors.append("compaction event must include integer token counts")
    unsettled = sorted(call_id for call_id, state in states.items() if state == "started")
    if unsettled:
        errors.append(f"unsettled tool lifecycle: {unsettled}")
    if any(event.get("type") == "runtime_exception" for event in trace):
        errors.append("runtime recorded an exception")
    result = _result(errors)
    result.update(
        {
            "provider_errors": provider_errors,
            "tool_errors": tool_errors,
            "interruptions": interruptions,
            "duplicate_tool_ids": duplicate_tool_ids,
            "compaction_events": compactions,
        }
    )
    return result


def _security_gate(manifest: CaseManifest, trace_path: Path) -> dict[str, object]:
    content = trace_path.read_text(encoding="utf-8")
    leaked = [value for value in manifest.private_values if value in content]
    if leaked:
        return _failed("portable trace contains a registered private value")
    if '"network_policy":"disabled"' not in content.replace(" ", ""):
        return _failed("trace does not declare the offline network policy")
    return _result([])


def _delivery_gate(manifest: CaseManifest, trace: list[dict[str, Any]]) -> dict[str, object]:
    delivery = next((event.get("data", {}) for event in reversed(trace) if event.get("type") == "final_delivery"), {})
    if not isinstance(delivery, dict):
        return _failed("final delivery event is missing")
    if delivery.get("completed") is not manifest.expected_delivery_completed:
        return _failed(
            f"final delivery completion differs: expected {manifest.expected_delivery_completed}, got {delivery.get('completed')}"
        )
    content = delivery.get("content", "")
    if manifest.expected_delivery_contains not in content:
        return _failed("final delivery does not contain the required completion text")
    return _result([])


def _result(errors: list[str]) -> dict[str, object]:
    return {"passed": not errors, "errors": errors}


def _failed(error: str) -> dict[str, object]:
    return _result([error])
