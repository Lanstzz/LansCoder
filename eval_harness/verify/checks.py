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
        "artifact": _artifact_gate(manifest, Path(artifacts_path)),
        "recovery": _recovery_gate(trace),
        "security": _security_gate(manifest, Path(trace_path)),
        "delivery": _delivery_gate(manifest, trace),
    }


def build_scorecard(verification: dict[str, dict[str, object]], trace_path: str | Path) -> dict[str, object]:
    """Create a portable scorecard that separates hard gates from metrics."""

    trace = load_trace(trace_path)
    type_counts = Counter(str(event.get("type")) for event in trace)
    completed = next((event.get("data", {}) for event in reversed(trace) if event.get("type") == "run_completed"), {})
    if not isinstance(completed, dict):
        completed = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": all(bool(gate.get("passed")) for gate in verification.values()),
        "gates": verification,
        "metrics": {
            "provider_calls": type_counts["provider_request"],
            "tool_calls": type_counts["tool_execution_start"],
            "elapsed_ms": completed.get("elapsed_ms"),
            "context_compactions": completed.get("compaction_events", 0),
            "token_usage": None,
        },
    }


def compare_scorecards(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, object]:
    """Compare gate regressions and numeric metrics without a language-model judge."""

    baseline_gates = baseline.get("gates", {})
    current_gates = current.get("gates", {})
    regressions = sorted(
        gate
        for gate, result in current_gates.items()
        if isinstance(result, dict)
        and not result.get("passed")
        and isinstance(baseline_gates.get(gate), dict)
        and baseline_gates[gate].get("passed")
    )
    baseline_metrics = baseline.get("metrics", {})
    current_metrics = current.get("metrics", {})
    deltas = {
        key: current_metrics[key] - baseline_metrics[key]
        for key in set(baseline_metrics) & set(current_metrics)
        if isinstance(baseline_metrics[key], (int, float)) and isinstance(current_metrics[key], (int, float))
    }
    return {"passed": not regressions, "gate_regressions": regressions, "metric_deltas": deltas}


def _trace_gate(trace: list[dict[str, Any]]) -> dict[str, object]:
    errors: list[str] = []
    if not trace:
        return _failed("trace is empty")
    for expected_sequence, event in enumerate(trace, start=1):
        if event.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"sequence {expected_sequence} has an unsupported schema version")
        if event.get("sequence") != expected_sequence:
            errors.append(f"sequence is not contiguous at event {expected_sequence}")
    required = {"run_started", "user_input", "provider_request", "provider_response", "artifacts", "final_delivery", "run_completed", "trace_integrity"}
    actual = {str(event.get("type")) for event in trace}
    missing = sorted(required - actual)
    if missing:
        errors.append(f"missing required events: {', '.join(missing)}")
    integrity = trace[-1] if trace else {}
    if integrity.get("type") != "trace_integrity":
        errors.append("trace_integrity must be the final event")
    else:
        data = integrity.get("data", {})
        expected_digest = data.get("digest") if isinstance(data, dict) else None
        if expected_digest != trace_digest(trace[:-1]):
            errors.append("trace integrity digest does not match the preceding events")
    return _result(errors)


def _artifact_gate(manifest: CaseManifest, artifacts_path: Path) -> dict[str, object]:
    errors: list[str] = []
    for relative_path, expected_content in manifest.expected_artifacts.items():
        target = artifacts_path / relative_path
        if not target.is_file():
            errors.append(f"required artifact is missing: {relative_path}")
        elif target.read_text(encoding="utf-8") != expected_content:
            errors.append(f"artifact content differs: {relative_path}")
    return _result(errors)


def _recovery_gate(trace: list[dict[str, Any]]) -> dict[str, object]:
    starts: set[str] = set()
    ends: set[str] = set()
    errors: list[str] = []
    for event in trace:
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        call_id = data.get("tool_call_id")
        if not isinstance(call_id, str):
            continue
        if event.get("type") == "tool_execution_start":
            starts.add(call_id)
        elif event.get("type") == "tool_execution_end":
            ends.add(call_id)
    if starts != ends:
        errors.append(f"unsettled tool lifecycle: started={sorted(starts)} ended={sorted(ends)}")
    if any(event.get("type") == "runtime_exception" for event in trace):
        errors.append("runtime recorded an exception")
    return _result(errors)


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
    content = delivery.get("content", "") if isinstance(delivery, dict) else ""
    if manifest.expected_delivery_contains not in content:
        return _failed("final delivery does not contain the required completion text")
    return _result([])


def _result(errors: list[str]) -> dict[str, object]:
    return {"passed": not errors, "errors": errors}


def _failed(error: str) -> dict[str, object]:
    return _result([error])
