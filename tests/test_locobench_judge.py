"""benchmark.locobench.judge 纯函数单测(prompt 构造/JSON 解析/归一化,不发网络请求)。"""

from __future__ import annotations

from benchmark.locobench.judge import (
    build_judge_messages,
    normalize_judge_output,
    parse_judge_json,
)


def _scenario_meta() -> dict:
    return {
        "original_scenario": {
            "task_prompt": "Trace the data flow from network to canvas.",
            "evaluation_criteria": ["Correctness of Trace", "Component Identification"],
            "ground_truth": "`service_realtime.h` then `event_bus_publish()`.",
        }
    }


def test_build_judge_messages_includes_task_criteria_answers() -> None:
    transcript = {
        "turn_stats": [
            {"turn": 1, "assistant_content": "I traced the flow.", "tool_calls": [{"function_name": "read_file"}]},
            {"turn": 2, "assistant_content": "", "tool_calls": [{"function_name": "file_system_read_file"}]},
        ]
    }
    messages = build_judge_messages(_scenario_meta(), transcript, ["docs/a.md"])
    assert len(messages) == 2
    user = messages[1]["content"]
    assert "Trace the data flow from network to canvas." in user
    assert "Correctness of Trace" in user
    assert "service_realtime.h" in user
    assert "I traced the flow." in user
    assert "file_system_read_file" in user
    assert "docs/a.md" in user
    assert "# Tool functions used" in user


def test_build_judge_messages_truncates_long_answers() -> None:
    long_answer = "x" * 5000
    transcript = {"turn_stats": [{"turn": 1, "assistant_content": long_answer, "tool_calls": []}]}
    messages = build_judge_messages(_scenario_meta(), transcript, [], max_answer_chars=100)
    assert len(messages[1]["content"]) < 1200
    assert "[truncated]" in messages[1]["content"]


def test_parse_judge_json_fenced_and_noisy() -> None:
    assert parse_judge_json('{"adherence": 0.8}')["adherence"] == 0.8
    assert parse_judge_json('```json\n{"correctness": 0.7}\n```')["correctness"] == 0.7
    assert parse_judge_json('prefix {"a": 1} suffix')["a"] == 1


def test_normalize_judge_output_clamps_and_quality() -> None:
    data = {
        "criteria": [{"id": "1", "score": 1.2, "rationale": "ok"}, {"id": "2", "score": -0.1}],
        "adherence": 0.9,
        "correctness": 0.7,
        "rationale": "good",
    }
    out = normalize_judge_output(data, n_criteria=2)
    assert out["criteria"][0]["score"] == 1.0
    assert out["criteria"][1]["score"] == 0.0
    assert out["adherence"] == 0.9
    assert out["correctness"] == 0.7
    assert out["quality"] == 0.8
    assert out["criteria_mean"] == 0.5
    assert out["expected_criteria"] == 2
