"""Deterministic verification and machine-readable scorecard construction."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from eval_harness.schema.models import CaseManifest, SCHEMA_VERSION
from eval_harness.trace.canonicalize import load_trace
from eval_harness.trace.recorder import trace_digest


def verify_run(manifest: CaseManifest, trace_path: str | Path, artifacts_path: str | Path) -> dict[str, dict[str, object]]:
    """Evaluate the five hard gates without reading hidden verifier inputs."""

    trace = load_trace(trace_path)
    return {
        "trace": _trace_gate(trace),
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


def _trace_gate(trace: list[dict[str, Any]]) -> dict[str, object]:
    errors: list[str] = []
    if not trace:
        return _failed("trace is empty")
    for expected_sequence, event in enumerate(trace, start=1):
        if event.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"sequence {expected_sequence} has an unsupported schema version")
        if event.get("sequence") != expected_sequence:
            errors.append(f"sequence is not contiguous at event {expected_sequence}")
    required = {"run_started", "user_input", "provider_request", "artifacts", "final_delivery", "run_completed", "trace_integrity"}
    actual = {str(event.get("type")) for event in trace}
    missing = sorted(required - actual)
    if missing:
        errors.append(f"missing required events: {', '.join(missing)}")
    if "provider_response" not in actual and "provider_error" not in actual:
        errors.append("missing provider response or provider error event")
    integrity = trace[-1] if trace else {}
    if integrity.get("type") != "trace_integrity":
        errors.append("trace_integrity must be the final event")
    else:
        data = integrity.get("data", {})
        expected_digest = data.get("digest") if isinstance(data, dict) else None
        if expected_digest != trace_digest(trace[:-1]):
            errors.append("trace integrity digest does not match the preceding events")
        if isinstance(data, dict) and data.get("event_count") != len(trace) - 1:
            errors.append("trace integrity event_count does not match the trace prefix")
    return _result(errors)


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
