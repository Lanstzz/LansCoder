from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_harness.replay.runner import run_offline_case_path
from eval_harness.trace.canonicalize import canonical_json, load_trace
from eval_harness.trace.recorder import TraceRecorder
from eval_harness.trace.redaction import Redactor


CASE_PATH = Path(__file__).parents[1] / "eval_harness" / "cases" / "offline" / "write_greeting.json"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_offline_case_produces_verified_trace_scorecard_and_artifact(tmp_path: Path) -> None:
    result = await run_offline_case_path(CASE_PATH, tmp_path / "run")

    assert result.trace_path.is_file()
    assert result.scorecard_path.is_file()
    assert (result.artifacts_path / "greeting.txt").read_text(encoding="utf-8") == "hello from harness\n"
    assert result.scorecard["passed"] is True
    assert all(gate["passed"] for gate in result.scorecard["gates"].values())

    events = load_trace(result.trace_path)
    event_types = [event["type"] for event in events]
    assert event_types[0] == "run_started"
    assert event_types[-1] == "trace_integrity"
    assert {"provider_request", "provider_response", "tool_execution_start", "tool_execution_end", "artifacts", "final_delivery"} <= set(event_types)
    completed = next(event["data"] for event in events if event["type"] == "run_completed")
    assert completed["failure"] is None
    assert completed["compaction_events"] == 0
    assert completed["recovery_events"] == []
    portable_trace = result.trace_path.read_text(encoding="utf-8")
    assert "Role and instruction priority" not in portable_trace
    assert "Create only genuinely new tasks" not in portable_trace
    assert "wrote greeting.txt" not in portable_trace


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_repeated_offline_case_has_a_stable_canonical_trace(tmp_path: Path) -> None:
    first = await run_offline_case_path(CASE_PATH, tmp_path / "first")
    second = await run_offline_case_path(CASE_PATH, tmp_path / "second")

    assert canonical_json(load_trace(first.trace_path)) == canonical_json(load_trace(second.trace_path))


def test_portable_trace_redacts_registered_values_and_absolute_paths(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path / "run", redactor=Redactor(sensitive_values=["test-private-value"], paths=[tmp_path]))
    recorder.record("user_input", {"content": f"token=test-private-value path={tmp_path}"})
    trace_path = recorder.write()
    portable = trace_path.read_text(encoding="utf-8")

    assert "test-private-value" not in portable
    assert str(tmp_path) not in portable
    assert "<REDACTED:" in portable
    assert "<PATH:" in portable


def test_scorecard_is_machine_readable_json(tmp_path: Path) -> None:
    scorecard_path = tmp_path / "scorecard.json"
    scorecard_path.write_text(json.dumps({"passed": True}), encoding="utf-8")
    assert json.loads(scorecard_path.read_text(encoding="utf-8")) == {"passed": True}
