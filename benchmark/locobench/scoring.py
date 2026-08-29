"""Phase 3.5 打分:A(确定性引用检查)+ C(harness 既有分)加权,可选接入 B(LLM judge)。

目标:对"不跑偏 / 结果正确"给出加权得分,三 tier:

- **A 确定性引用检查**(纯规则,零成本):从场景 ``original_scenario.ground_truth``
  抽取反引号关键事实(文件/函数/事件名),检查 agent 最终回答的命中率;
  另含 engagement(是否产出非空最终回答 + 是否调用过工具)。
- **B LLM-as-judge**(可选):``judge.py`` 用不同模型(默认 dashscope qwen3.7-plus)
  对照 ``task_prompt + evaluation_criteria + ground_truth`` 盲评,输出
  adherence(不跑偏)+ correctness(结果正确)。
- **C harness 既有分**:只取 ``LCBA-Comp``(理解/正确性方向);``LCBA-Eff``
  单独报告不并入。

加权默认 ``A 0.3 / B 0.5 / C 0.2``(可 CLI 覆盖);B 缺失时 A/C 重新归一。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_WEIGHTS: dict[str, float] = {"a": 0.3, "b": 0.5, "c": 0.2}

_BACKTICK_RE = re.compile(r"`([^`]+)`")


# -- A tier: deterministic reference check ---------------------------------

def _keep_fact(fact: str) -> bool:
    """过滤太短/太常见的标识符:保留带 . _ 或驼峰、或足够长的。"""

    if len(fact) >= 6:
        return True
    if len(fact) >= 3 and (
        "." in fact or "_" in fact or any(char.isupper() for char in fact[1:])
    ):
        return True
    return False


def _normalize_fact(fact: str) -> str:
    return fact.rstrip("()").strip()


def _flatten_strings(value: Any) -> list[str]:
    """递归收集 dict/list 里所有字符串叶子(ground_truth 有 str/dict 两种形态)。"""

    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out
    return []


def extract_ground_truth_facts(ground_truth: str | dict[str, Any] | list[Any] | None) -> list[str]:
    """从 ground_truth(str 或 dict)抽取反引号关键事实(去重、规范化)。"""

    texts = _flatten_strings(ground_truth)
    facts: list[str] = []
    for text in texts:
        for match in _BACKTICK_RE.finditer(text or ""):
            fact = _normalize_fact(match.group(1))
            if fact and _keep_fact(fact) and fact not in facts:
                facts.append(fact)
    return facts


def fact_hit_rate(answers: list[str], facts: list[str]) -> tuple[int, int, float]:
    """最终回答文本对关键事实的命中率(子串匹配,大小写不敏感)。"""

    if not facts:
        return 0, 0, 1.0
    joined = " ".join(answers or []).lower()
    hits = sum(1 for fact in facts if fact.lower() in joined)
    return hits, len(facts), round(hits / len(facts), 4)


def tier_a(scenario_meta: dict[str, Any], transcript: dict[str, Any]) -> dict[str, Any]:
    """A 层:ground_truth 关键事实命中 + engagement。"""

    original = scenario_meta.get("original_scenario") or {}
    facts = extract_ground_truth_facts(original.get("ground_truth"))
    turns = transcript.get("turn_stats") or []
    answers = [
        str(turn.get("assistant_content") or "")
        for turn in turns
        if str(turn.get("assistant_content") or "").strip()
    ]
    last_answer = answers[-1] if answers else ""
    hits, total, correctness = fact_hit_rate(answers, facts)
    used_tools = any(turn.get("tool_calls") for turn in turns)
    engagement = 1.0 if (len(last_answer.strip()) >= 50 and used_tools) else 0.0
    return {
        "facts_total": total,
        "facts_hit": hits,
        "correctness": correctness,
        "engagement": engagement,
        "answers_checked": len(answers),
    }


# -- C tier: harness existing score ----------------------------------------

def tier_c(result: dict[str, Any]) -> dict[str, Any]:
    """C 层:复用 harness 分;只把 LCBA-Comp 并入加权,Eff 单独报告。"""

    return {
        "lcba_comprehension": round(float(result.get("lcba_comprehension") or 0), 4),
        "lcba_efficiency": round(float(result.get("lcba_efficiency") or 0), 4),
        "overall": round(float(result.get("overall_score") or 0), 4),
    }


# -- weighted combine ------------------------------------------------------

def combine(
    a: dict[str, Any],
    c: dict[str, Any],
    b: dict[str, Any] | None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """加权总分:0.3*A_correctness + 0.5*B_quality + 0.2*C_comp。

    B 缺失时(仅 A+C)按 A/C 权重重新归一,并在结果里标注。
    """

    w = dict(weights or DEFAULT_WEIGHTS)
    a_score = float(a.get("correctness") or 0.0)
    c_score = float(c.get("lcba_comprehension") or 0.0)

    if b is None:
        total = (w["a"] * a_score + w["c"] * c_score) / (w["a"] + w["c"])
        return {
            "total": round(total, 4),
            "a": a_score,
            "b": None,
            "c": c_score,
            "b_quality": None,
            "weights_used": {"a": round(w["a"] / (w["a"] + w["c"]), 4), "c": round(w["c"] / (w["a"] + w["c"]), 4)},
            "note": "B(judge)未提供,总分按 A/C 归一;最终口径应为三 tier 加权",
        }

    b_score = float(b.get("quality") or 0.0)
    total = w["a"] * a_score + w["b"] * b_score + w["c"] * c_score
    return {
        "total": round(total, 4),
        "a": a_score,
        "b": b_score,
        "c": c_score,
        "b_quality": b_score,
        "weights_used": dict(w),
        "note": "总分 = 0.3*A_correctness + 0.5*B_quality(0.5*adherence+0.5*correctness) + 0.2*C_comp",
    }


# -- loaders ---------------------------------------------------------------

def load_scenario_meta(locobench_root: Path, scenario_id: str) -> dict[str, Any]:
    path = locobench_root / "data" / "output" / "agent_scenarios" / f"{scenario_id}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    transcript = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    first_agent_results = next(iter(results.values()))
    result = first_agent_results[0]
    scenario = transcript["scenarios"][0]
    return result, scenario


def score_run(
    run_dir: Path,
    locobench_root: Path,
    b: dict[str, Any] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    result, scenario = load_run(run_dir)
    scenario_meta = load_scenario_meta(locobench_root, str(scenario.get("scenario_id") or ""))
    a = tier_a(scenario_meta, scenario)
    c = tier_c(result)
    final = combine(a, c, b, weights)
    return {
        "run": str(run_dir),
        "scenario_id": scenario.get("scenario_id"),
        "compaction_strategy": scenario.get("compaction_strategy"),
        "session_id": scenario.get("session_id"),
        "tier_a": a,
        "tier_b": b,
        "tier_c": c,
        "final": final,
    }


# -- CLI -------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoCoBench × LansCoder Phase 3.5 打分(A+C,可选 B)")
    parser.add_argument("--run", required=True, help="run 目录(benchmark/runs/locobench/<run>)")
    parser.add_argument("--locobench-root", required=True, help="LoCoBench-Agent clone 目录")
    parser.add_argument("--judge-result", help="judge.py 产出的 B 层 JSON(scorecard-b.json)")
    parser.add_argument("--weights", nargs=3, type=float, default=None, metavar=("WA", "WB", "WC"), help="A/B/C 权重(默认 0.3 0.5 0.2)")
    parser.add_argument("--output", help="scorecard.json 输出路径(缺省打印)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    weights = {"a": args.weights[0], "b": args.weights[1], "c": args.weights[2]} if args.weights else None
    b = None
    if args.judge_result:
        b = json.loads(Path(args.judge_result).read_text(encoding="utf-8"))
    scorecard = score_run(
        Path(args.run).resolve(),
        Path(args.locobench_root).resolve(),
        b=b,
        weights=weights,
    )
    if args.output:
        out = Path(args.output)
        out.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"scorecard.json 已写入 {out}")
    else:
        print(json.dumps(scorecard, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
