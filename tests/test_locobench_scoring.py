"""benchmark.locobench.scoring 纯函数单测(Phase 3.5 A/C/加权,不依赖网络/locobench)。"""

from __future__ import annotations

from benchmark.locobench.scoring import (
    combine,
    extract_ground_truth_facts,
    fact_hit_rate,
    tier_a,
    tier_c,
)


def test_extract_ground_truth_facts_backticks() -> None:
    gt = (
        "Uses `service_realtime.h` and calls `event_bus_publish()` with "
        "`EVENT_CANVAS_ITEM_MOVED`; `vm_canvas.c` subscribes. Also mentions `the`."
    )
    facts = extract_ground_truth_facts(gt)
    assert "service_realtime.h" in facts
    assert "event_bus_publish" in facts  # () 被去掉
    assert "EVENT_CANVAS_ITEM_MOVED" in facts
    assert "vm_canvas.c" in facts
    assert "the" not in facts  # 太短被过滤


def test_fact_hit_rate_case_insensitive_substring() -> None:
    facts = ["service_realtime.h", "event_bus_publish", "EVENT_CANVAS_ITEM_MOVED"]
    answers = ["The SERVICE_REALTIME.H file calls event_bus_publish with EVENT_CANVAS_ITEM_MOVED."]
    hits, total, rate = fact_hit_rate(answers, facts)
    assert (hits, total) == (3, 3)
    assert rate == 1.0


def test_fact_hit_rate_empty_facts_returns_one() -> None:
    hits, total, rate = fact_hit_rate(["anything"], [])
    assert (hits, total) == (0, 0)
    assert rate == 1.0


def test_tier_a_scores_correctness_and_engagement() -> None:
    meta = {
        "original_scenario": {
            "ground_truth": "key is `module_a.py` and `run_pipeline`; also `Config`",
        }
    }
    transcript = {
        "turn_stats": [
            {"turn": 1, "assistant_content": "", "tool_calls": [{"function_name": "read_file"}]},
            {"turn": 2, "assistant_content": "I analyzed module_a.py; run_pipeline is the entry; Config holds params.", "tool_calls": []},
        ]
    }
    a = tier_a(meta, transcript)
    assert a["correctness"] == 1.0
    assert a["engagement"] == 1.0
    assert a["facts_total"] == 3
    assert a["answers_checked"] == 1


def test_tier_c_reuses_harness_comp_only() -> None:
    c = tier_c({"lcba_comprehension": 0.73, "lcba_efficiency": 0.6, "overall_score": 0.68})
    assert c["lcba_comprehension"] == 0.73
    assert c["lcba_efficiency"] == 0.6
    assert c["overall"] == 0.68


def test_combine_three_tiers_weighted() -> None:
    a = {"correctness": 0.8}
    c = {"lcba_comprehension": 0.7}
    b = {"quality": 0.6}
    final = combine(a, c, b)
    assert final["a"] == 0.8
    assert final["b"] == 0.6
    assert final["c"] == 0.7
    assert final["total"] == round(0.1 * 0.8 + 0.6 * 0.6 + 0.3 * 0.7, 4)
    assert final["weights_used"] == {"a": 0.1, "b": 0.6, "c": 0.3}


def test_combine_without_b_normalizes_ac() -> None:
    a = {"correctness": 0.8}
    c = {"lcba_comprehension": 0.7}
    final = combine(a, c, None)
    assert final["b"] is None
    assert final["total"] == round((0.1 * 0.8 + 0.3 * 0.7) / 0.4, 4)
    assert final["note"].startswith("B(judge)未提供")
