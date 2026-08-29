"""Phase 3.5 B 层:LLM-as-judge(对照 task/evaluation_criteria/ground_truth 打分)。

- 用与 agent **不同** 的模型(默认 dashscope ``qwen3.7-plus``),避免自评。
- 输入"瘦身":只喂 task_prompt + evaluation_criteria + ground_truth(截断)
  + 每回合最终回答(截断)+ 工具名 + 改动文件,**不喂完整 tool_result**。
- 输出结构化 JSON:temperature=0;逐条 criterion 0-1 + adherence(不跑偏)
  + correctness(结果正确)+ 理由。
- ``quality = 0.5*adherence + 0.5*correctness`` 供 scoring.py 加权。

直连 DashScope OpenAI-compatible 端点(``/compatible-mode/v1``),API key
从环境变量读取(默认 ``QWEN_API_KEY`` / ``DASHSCOPE_API_KEY``),不落盘。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from benchmark.locobench.scoring import load_run, load_scenario_meta

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.7-plus"
SYSTEM_PROMPT = (
    "You are an impartial task evaluator for a software-engineering coding agent benchmark. "
    "Score the agent's work against the given task, evaluation criteria, and reference ground truth. "
    "Be strict and evidence-based: base scores on what the agent actually said and did. "
    'Reply with ONLY a JSON object of the form '
    '{"criteria":[{"id":"<n>","score":0.0,"rationale":"..."}],"adherence":0.0,"correctness":0.0,"rationale":"..."}. '
    "All scores are floats in [0,1]. adherence = how well the agent stayed on task (no drift, addressed the task). "
    "correctness = how correct/complete the final result is against the reference ground truth."
)


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def build_judge_messages(
    scenario_meta: dict[str, Any],
    transcript: dict[str, Any],
    modified_files: list[str],
    *,
    max_answer_chars: int = 4_000,
    max_ground_truth_chars: int = 4_000,
) -> list[dict[str, str]]:
    """构造 judge 消息(输入已瘦身:不含工具结果原文)。"""

    original = scenario_meta.get("original_scenario") or {}
    task_prompt = original.get("task_prompt") or scenario_meta.get("description") or ""
    criteria = original.get("evaluation_criteria") or []
    ground_truth = original.get("ground_truth") or ""

    turns = transcript.get("turn_stats") or []
    answers: list[dict[str, str]] = []
    for turn in turns:
        content = str(turn.get("assistant_content") or "").strip()
        if content:
            answers.append({"turn": str(turn.get("turn")), "content": _truncate(content, max_answer_chars)})

    tool_names: list[str] = []
    for turn in turns:
        for call in turn.get("tool_calls") or []:
            name = str(call.get("function_name") or call.get("tool_name") or "")
            if name and name not in tool_names:
                tool_names.append(name)

    criteria_block = "\n".join(f"{i}. {item}" for i, item in enumerate(criteria, 1)) if criteria else "(none provided)"
    answers_block = "\n\n".join(
        f"--- Turn {a['turn']} ---\n{a['content']}" for a in answers
    ) or "(no final answer produced)"

    user = (
        "# Task\n"
        f"{task_prompt}\n\n"
        "# Evaluation criteria\n"
        f"{criteria_block}\n\n"
        "# Reference ground truth (for correctness check)\n"
        f"{_truncate(ground_truth, max_ground_truth_chars)}\n\n"
        "# Agent final answers (per turn)\n"
        f"{answers_block}\n\n"
        "# Tool functions used\n"
        f"{', '.join(tool_names) or '(none)'}\n\n"
        "# Files written\n"
        f"{', '.join(modified_files) or '(none)'}\n\n"
        "Now score the agent: one 0-1 score per evaluation criterion, "
        "plus overall adherence (stayed on task, addressed the task, no drift) "
        "and correctness (final result matches reference ground truth). "
        "Return the JSON object only."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_judge_json(content: str) -> dict[str, Any]:
    """稳健解析 judge 返回的 JSON(容忍 ```json 围栏/前后噪声)。"""

    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"judge 返回不是 JSON: {content[:200]!r}")
        data = json.loads(content[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("judge 返回 JSON 不是对象")
    return data


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(1.0, max(0.0, number)), 4)


def normalize_judge_output(data: dict[str, Any], n_criteria: int) -> dict[str, Any]:
    """归一化 judge 输出:clamp 分数、算 quality = 0.5*adherence + 0.5*correctness。"""

    criteria_raw = data.get("criteria") or []
    criteria: list[dict[str, Any]] = []
    for index, item in enumerate(criteria_raw, 1):
        if isinstance(item, dict):
            criteria.append(
                {
                    "id": str(item.get("id") or index),
                    "score": _clamp(item.get("score")),
                    "rationale": str(item.get("rationale") or "")[:500],
                }
            )
    adherence = _clamp(data.get("adherence"))
    correctness = _clamp(data.get("correctness"))
    criteria_mean = round(sum(c["score"] for c in criteria) / len(criteria), 4) if criteria else None
    return {
        "adherence": adherence,
        "correctness": correctness,
        "quality": round(0.5 * adherence + 0.5 * correctness, 4),
        "criteria": criteria,
        "criteria_mean": criteria_mean,
        "rationale": str(data.get("rationale") or "")[:1000],
        "expected_criteria": n_criteria,
    }


def call_judge(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    timeout: float = 300.0,
    json_mode: bool = True,
) -> dict[str, Any]:
    """调用 OpenAI-compatible 端点(dashscope /compatible-mode/v1),返回解析后的 JSON。

    ``json_mode=True`` 时带 ``response_format: json_object``;个别供应商/模型
    不支持时可关掉(输出仍由 ``parse_judge_json`` 兜底提取 JSON)。
    """

    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return parse_judge_json(content)


def judge_run(
    run_dir: Path,
    locobench_root: Path,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    temperature: float = 0.0,
    json_mode: bool = True,
) -> dict[str, Any]:
    result, scenario = load_run(run_dir)
    scenario_meta = load_scenario_meta(locobench_root, str(scenario.get("scenario_id") or ""))
    original = scenario_meta.get("original_scenario") or {}
    n_criteria = len(original.get("evaluation_criteria") or [])
    messages = build_judge_messages(scenario_meta, scenario, list(result.get("modified_files") or []))
    resolved_key = api_key or os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not resolved_key:
        raise RuntimeError("缺少 judge API key:设置 QWEN_API_KEY 或 DASHSCOPE_API_KEY")
    data = call_judge(base_url, resolved_key, model, messages, temperature=temperature, json_mode=json_mode)
    return {
        "run": str(run_dir),
        "scenario_id": scenario.get("scenario_id"),
        "model": model,
        "base_url": base_url,
        "temperature": temperature,
        "json_mode": json_mode,
        **normalize_judge_output(data, n_criteria),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoCoBench × LansCoder Phase 3.5 B 层 LLM judge")
    parser.add_argument("--run", required=True, help="run 目录(benchmark/runs/locobench/<run>)")
    parser.add_argument("--locobench-root", required=True, help="LoCoBench-Agent clone 目录")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"judge 模型(默认 {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible base url")
    parser.add_argument("--api-key-env", default="QWEN_API_KEY", help="API key 环境变量名")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--json-mode", dest="json_mode", action=argparse.BooleanOptionalAction, default=True, help="带 response_format=json_object(默认开;个别模型不支持时用 --no-json-mode)")
    parser.add_argument("--output", help="scorecard-b.json 输出路径(缺省打印)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get(args.api_key_env)
    scorecard = judge_run(
        Path(args.run).resolve(),
        Path(args.locobench_root).resolve(),
        model=args.model,
        base_url=args.base_url,
        api_key=api_key,
        temperature=args.temperature,
        json_mode=args.json_mode,
    )
    if args.output:
        out = Path(args.output)
        out.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"scorecard-b.json 已写入 {out}")
    else:
        print(json.dumps(scorecard, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
