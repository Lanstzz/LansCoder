"""benchmark.locobench.analyze 纯函数单测(不依赖 locobench)。"""

from __future__ import annotations

from benchmark.locobench.analyze import analyze, compaction_stats, harness_heuristic, lanscoder_chars4, provider_usage


def _event(event_type: str, **kwargs) -> dict:
    record: dict = {"type": event_type}
    record.update(kwargs)
    return record


def test_compaction_stats_counts_rates_and_tokens() -> None:
    events = [
        _event(
            "compaction_completed",
            before_tokens=1000,
            after_tokens=400,
            level_metrics={
                "l1": {"changed_parts": 2},
                "l2": {"changed_parts": 0},
            },
        ),
        _event(
            "compaction_completed",
            before_tokens=800,
            after_tokens=300,
            level_metrics={
                "l1": {"changed_parts": 0},
                "l2": {"changed_parts": 1},
            },
        ),
        _event("llm_compaction_completed", status="success", hard_truncate=False),
        _event("llm_compaction_completed", status="success", hard_truncate=True),
        _event("llm_compaction_completed", status="failed", hard_truncate=False),
        _event("compaction_skipped", reason="under_threshold"),
    ]
    stats = compaction_stats(events)

    assert stats["attempts"] == 2
    assert stats["l1_hits"] == 1
    assert stats["l1_hit_rate"] == 0.5
    assert stats["l2_hits"] == 1
    assert stats["l2_hit_rate"] == 0.5
    assert stats["l3_success"] == 1
    assert stats["l3_success_rate"] == 0.5
    assert stats["l3_failure"] == 1
    assert stats["hard_truncate"] == 1
    assert stats["hard_truncate_rate"] == 0.5
    assert stats["skipped_events"] == 1
    assert stats["before_tokens_sum"] == 1800
    assert stats["after_tokens_sum"] == 700


def test_compaction_stats_returns_none_rates_when_no_attempts() -> None:
    stats = compaction_stats([_event("compaction_skipped", reason="strategy_no_compact")])
    assert stats["attempts"] == 0
    assert stats["l1_hit_rate"] is None
    assert stats["l3_success_rate"] is None
    assert stats["hard_truncate_rate"] is None


def _scenario(**overrides) -> dict:
    scenario = {
        "scenario_id": "scn_1",
        "session_id": "sess_1",
        "turn_stats": [
            {
                "turn": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "tokens_used": 120,
                "harness_context_tokens": 30,
                "harness_total_context_tokens": 80,
                "lanscoder_chars4_estimate": 200,
            },
            {
                "turn": 2,
                "input_tokens": 150,
                "output_tokens": 30,
                "tokens_used": 180,
                "harness_context_tokens": 40,
                "harness_total_context_tokens": 130,
                "lanscoder_chars4_estimate": 320,
            },
        ],
        "compaction_events": [],
    }
    scenario.update(overrides)
    return scenario


def test_provider_usage_sums_real_usage() -> None:
    assert provider_usage(_scenario()) == {
        "input_tokens": 250,
        "output_tokens": 50,
        "total_tokens": 300,
    }


def test_harness_heuristic_uses_labeled_heuristic() -> None:
    assert harness_heuristic(_scenario()) == {
        "assistant_context_tokens": 70,
        "final_total_context_tokens": 130,
    }


def test_lanscoder_chars4_series() -> None:
    assert lanscoder_chars4(_scenario()) == {"per_turn": [200, 320], "last": 320, "max": 320}


def test_analyze_aggregates_summary() -> None:
    result = analyze({"scenarios": [_scenario()]})
    assert len(result["per_scenario"]) == 1
    summary = result["summary"]
    assert summary["scenarios"] == 1
    assert summary["turns"] == 2
    assert summary["provider_usage"]["total_tokens"] == 300
    assert summary["harness_heuristic"]["assistant_context_tokens"] == 70
    assert summary["lanscoder_chars4"]["last"] == 320
    assert summary["compaction"]["attempts"] == 0
