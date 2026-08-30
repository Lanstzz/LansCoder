from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_harness.replay.runner import run_offline_case_path
from eval_harness.trace.canonicalize import canonical_json, load_trace
from eval_harness.trace.recorder import TraceRecorder
from eval_harness.trace.redaction import Redactor
from eval_harness.verify.checks import compare_scorecards


CASE_PATH = Path(__file__).parents[1] / "eval_harness" / "cases" / "offline" / "write_greeting.json"
CASES_DIR = CASE_PATH.parent


def test_offline_case_catalog_has_ten_small_deterministic_cases() -> None:
    cases = sorted(CASES_DIR.glob("*.json"))
    assert len(cases) >= 10
    assert {case.stem for case in cases} >= {
        "write_greeting",
        "no_tool_completion",
        "write_single",
        "write_two_files",
        "modify_existing",
        "tool_failure_retry",
        "duplicate_write",
        "nested_unicode",
        "overwrite_existing",
        "multi_round",
        "unauthorized_path",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("case_path", sorted(CASES_DIR.glob("*.json")), ids=lambda path: path.stem)
async def test_every_offline_case_produces_a_passing_scorecard(case_path: Path, tmp_path: Path) -> None:
    first = await run_offline_case_path(case_path, tmp_path / "first" / case_path.stem)
    second = await run_offline_case_path(case_path, tmp_path / "second" / case_path.stem)
    assert first.scorecard["passed"] is True
    assert all(gate["passed"] for gate in first.scorecard["gates"].values())
    assert canonical_json(load_trace(first.trace_path)) == canonical_json(load_trace(second.trace_path))


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


def _write_case(tmp_path: Path, **overrides: object) -> Path:
    case = {
        "schema_version": 1,
        "id": "probe",
        "title": "harness probe",
        "mode": "interaction_replay",
        "prompt": "Run the probe.",
        "fixture": None,
        "provider_tape": [{"content": "Probe completed.", "finish_reason": "stop", "tool_calls": []}],
        "expected_artifacts": {},
        "expected_delivery_contains": "Probe completed.",
    }
    case.update(overrides)
    path = tmp_path / "case.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    return path


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_provider_timeout_is_recorded_and_retried(tmp_path: Path) -> None:
    case_path = _write_case(
        tmp_path,
        provider_tape=[
            {"fault": "timeout"},
            {"content": "Recovered after provider timeout.", "finish_reason": "stop", "tool_calls": []},
        ],
        expected_delivery_contains="Recovered after provider timeout.",
    )

    result = await run_offline_case_path(case_path, tmp_path / "run")

    assert result.scorecard["passed"] is True
    assert result.scorecard["metrics"]["provider_errors"] == 1
    assert result.scorecard["gates"]["recovery"]["provider_errors"] == ["timeout"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_tool_timeout_is_a_closed_recoverable_lifecycle(tmp_path: Path) -> None:
    case_path = _write_case(
        tmp_path,
        prompt="Write timeout.txt, then report the result.",
        provider_tape=[
            {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "timed_out", "name": "write_file", "arguments": {"path": "timeout.txt", "content": "never\n"}}],
            },
            {"content": "Tool timeout was recovered.", "finish_reason": "stop", "tool_calls": []},
        ],
        tool_faults={"timed_out": "timeout"},
        expected_delivery_contains="Tool timeout was recovered.",
    )

    result = await run_offline_case_path(case_path, tmp_path / "run")

    assert result.scorecard["passed"] is True
    assert result.scorecard["gates"]["recovery"]["tool_errors"] == ["timeout"]
    assert not (result.artifacts_path / "timeout.txt").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_malformed_provider_response_is_categorized_without_trace_loss(tmp_path: Path) -> None:
    case_path = _write_case(tmp_path, provider_tape=[{"fault": "malformed_response"}])

    result = await run_offline_case_path(case_path, tmp_path / "run")

    assert result.scorecard["passed"] is False
    assert result.scorecard["gates"]["trace"]["passed"] is True
    assert result.scorecard["gates"]["recovery"]["provider_errors"] == ["malformed_response"]
    assert "runtime recorded an exception" in result.scorecard["gates"]["recovery"]["errors"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_compaction_probe_is_recorded_in_trace_and_metrics(tmp_path: Path) -> None:
    case_path = _write_case(tmp_path, enable_compaction=True)

    result = await run_offline_case_path(case_path, tmp_path / "run")
    events = load_trace(result.trace_path)

    assert result.scorecard["passed"] is True
    assert result.scorecard["metrics"]["context_compactions"] == 1
    assert result.scorecard["gates"]["recovery"]["compaction_events"] == 1
    assert next(event for event in events if event["type"] == "context_compaction")["data"]["noop"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_interrupt_probe_settles_tool_lifecycle(tmp_path: Path) -> None:
    case_path = _write_case(
        tmp_path,
        provider_tape=[
            {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "interrupt_me", "name": "write_file", "arguments": {"path": "partial.txt", "content": "written\n"}}],
            }
        ],
        interrupt_after_tool_calls=1,
        tool_faults={"interrupt_me": "interrupt"},
        expected_artifacts={},
        expected_delivery_contains="当前任务已中断。",
    )

    result = await run_offline_case_path(case_path, tmp_path / "run")

    assert result.scorecard["passed"] is True
    assert result.scorecard["gates"]["recovery"]["interruptions"] == ["interrupt_me"]
    assert result.scorecard["gates"]["recovery"]["errors"] == []
    assert any(event["type"] == "interrupt_requested" for event in load_trace(result.trace_path))


def test_compare_scorecards_flags_lost_hard_gate_and_reports_numeric_deltas() -> None:
    baseline = {"passed": True, "gates": {"trace": {"passed": True}}, "metrics": {"provider_calls": 2}}
    current = {"passed": False, "gates": {"trace": {"passed": False}}, "metrics": {"provider_calls": 3}}

    comparison = compare_scorecards(baseline, current)

    assert comparison == {
        "passed": False,
        "baseline_passed": True,
        "current_passed": False,
        "gate_regressions": ["trace"],
        "metric_deltas": {"provider_calls": 1},
    }
