"""LoCoBench × LansCoder 结果聚合分析(纯函数,不依赖 locobench)。

输入: ``driver.py`` 产出的 ``transcript.json``(scenarios 列表,每场景含
turn_stats 与 compaction_events)。输出 per-scenario + 汇总指标:

- 准确率/效率:直接来自 harness ``results.json``(本模块不重复计算)。
- 上下文规模:三套口径分开标注——harness 启发式 ``context_tokens``、
  provider 真实 usage、LansCoder ``chars/4`` 估算。
- 压缩行为:``CompactionEvent`` 统计(compaction_completed / L1/L2 hit rate、
  L3 success rate、硬截断率、before/after tokens)。

CLI::

    python -m benchmark.locobench.analyze --input <transcript.json> [--output analysis.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _level_changed(event: dict[str, Any], level: str) -> bool:
    metrics = event.get("level_metrics") or {}
    entry = metrics.get(level) or {}
    return bool(entry.get("changed_parts"))


def provider_usage(scenario: dict[str, Any]) -> dict[str, int]:
    """provider 真实 usage(回合级 usage 之和)。"""

    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for turn in scenario.get("turn_stats") or []:
        totals["input_tokens"] += int(_num(turn.get("input_tokens")))
        totals["output_tokens"] += int(_num(turn.get("output_tokens")))
        totals["total_tokens"] += int(_num(turn.get("tokens_used")))
    return totals


def harness_heuristic(scenario: dict[str, Any]) -> dict[str, int]:
    """harness 启发式 context_tokens(``len(split())*1.3``,非真实 token)。"""

    turns = scenario.get("turn_stats") or []
    assistant = sum(int(_num(turn.get("harness_context_tokens"))) for turn in turns)
    total = turns[-1].get("harness_total_context_tokens") if turns else 0
    return {
        "assistant_context_tokens": assistant,
        "final_total_context_tokens": int(_num(total)),
    }


def lanscoder_chars4(scenario: dict[str, Any]) -> dict[str, Any]:
    """LansCoder ``chars/4`` 估算(``(len+3)//4``)的回合级序列。"""

    series = [
        int(_num(turn.get("lanscoder_chars4_estimate")))
        for turn in scenario.get("turn_stats") or []
        if turn.get("lanscoder_chars4_estimate") is not None
    ]
    return {
        "per_turn": series,
        "last": series[-1] if series else None,
        "max": max(series) if series else None,
    }


def compaction_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """压缩行为统计:程序式(L1/L2)、L3、硬截断、skipped 与 before/after tokens。"""

    programmatic = [e for e in events if e.get("type") == "compaction_completed"]
    l3 = [e for e in events if e.get("type") == "llm_compaction_completed"]
    skipped = [e for e in events if e.get("type") == "compaction_skipped"]
    hard_truncate = [e for e in l3 if e.get("hard_truncate")]
    l3_success = [e for e in l3 if e.get("status") == "success" and not e.get("hard_truncate")]
    l3_failure = [e for e in l3 if e.get("status") != "success" and not e.get("hard_truncate")]
    l1_hits = sum(1 for e in programmatic if _level_changed(e, "l1"))
    l2_hits = sum(1 for e in programmatic if _level_changed(e, "l2"))
    attempts = len(programmatic)

    def rate(numerator: int) -> float | None:
        return round(numerator / attempts, 4) if attempts else None

    return {
        "attempts": attempts,
        "programmatic_events": attempts,
        "l1_hits": l1_hits,
        "l1_hit_rate": rate(l1_hits),
        "l2_hits": l2_hits,
        "l2_hit_rate": rate(l2_hits),
        "l3_events": len(l3),
        "l3_success": len(l3_success),
        "l3_success_rate": rate(len(l3_success)),
        "l3_failure": len(l3_failure),
        "l3_failure_rate": rate(len(l3_failure)),
        "hard_truncate": len(hard_truncate),
        "hard_truncate_rate": rate(len(hard_truncate)),
        "skipped_events": len(skipped),
        "before_tokens_sum": sum(int(_num(e.get("before_tokens"))) for e in programmatic),
        "after_tokens_sum": sum(int(_num(e.get("after_tokens"))) for e in programmatic),
    }


def analyze_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    events = scenario.get("compaction_events") or []
    turns = scenario.get("turn_stats") or []
    return {
        "scenario_id": scenario.get("scenario_id"),
        "session_id": scenario.get("session_id"),
        "turns": len(turns),
        "provider_usage": provider_usage(scenario),
        "harness_heuristic": harness_heuristic(scenario),
        "lanscoder_chars4": lanscoder_chars4(scenario),
        "compaction": compaction_stats(events),
    }


def _merge_compaction(total: dict[str, Any], per: dict[str, Any]) -> None:
    for key in (
        "attempts",
        "programmatic_events",
        "l1_hits",
        "l2_hits",
        "l3_events",
        "l3_success",
        "l3_failure",
        "hard_truncate",
        "skipped_events",
        "before_tokens_sum",
        "after_tokens_sum",
    ):
        total[key] = total.get(key, 0) + per[key]


def analyze(transcript: dict[str, Any]) -> dict[str, Any]:
    """对 transcript.json 的全部场景做聚合,返回 per-scenario + summary。"""

    scenarios = [analyze_scenario(s) for s in transcript.get("scenarios") or []]

    provider_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    harness_total = {"assistant_context_tokens": 0, "final_total_context_tokens": 0}
    compaction_total: dict[str, Any] = {
        "attempts": 0,
        "programmatic_events": 0,
        "l1_hits": 0,
        "l2_hits": 0,
        "l3_events": 0,
        "l3_success": 0,
        "l3_failure": 0,
        "hard_truncate": 0,
        "skipped_events": 0,
        "before_tokens_sum": 0,
        "after_tokens_sum": 0,
    }
    last_chars4: int | None = None
    max_chars4: int | None = None
    turns = 0
    for scenario in scenarios:
        for key in provider_total:
            provider_total[key] += scenario["provider_usage"][key]
        for key in harness_total:
            harness_total[key] += scenario["harness_heuristic"][key]
        _merge_compaction(compaction_total, scenario["compaction"])
        chars4 = scenario["lanscoder_chars4"]
        last_chars4 = chars4["last"] if chars4["last"] is not None else last_chars4
        if chars4["max"] is not None:
            max_chars4 = max(max_chars4 or 0, chars4["max"])
        turns += scenario["turns"]

    attempts = compaction_total.get("attempts", 0)

    def rate(key: str) -> float | None:
        return round(compaction_total.get(key, 0) / attempts, 4) if attempts else None

    summary = {
        "scenarios": len(scenarios),
        "turns": turns,
        "provider_usage": provider_total,
        "harness_heuristic": harness_total,
        "lanscoder_chars4": {"last": last_chars4, "max": max_chars4},
        "compaction": {
            **compaction_total,
            "l1_hit_rate": rate("l1_hits"),
            "l2_hit_rate": rate("l2_hits"),
            "l3_success_rate": rate("l3_success"),
            "l3_failure_rate": rate("l3_failure"),
            "hard_truncate_rate": rate("hard_truncate"),
        },
    }
    return {"per_scenario": scenarios, "summary": summary}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoCoBench × LansCoder 结果聚合分析")
    parser.add_argument("--input", required=True, help="transcript.json 路径")
    parser.add_argument("--output", help="analysis.json 输出路径(缺省只打印)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transcript = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = analyze(transcript)
    if args.output:
        out = Path(args.output)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"analysis.json 已写入 {out}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
