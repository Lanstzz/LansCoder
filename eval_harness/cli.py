"""Command-line entry point for offline evaluation runs and golden comparison data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval_harness.replay.runner import run_case_sync
from eval_harness.trace.canonicalize import canonical_json, load_trace
from eval_harness.verify.checks import compare_scorecards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_harness", description="Offline-first LansCoder evaluation harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run one offline interaction replay case")
    run_parser.add_argument("--case", type=Path, required=True, help="portable JSON case manifest")
    run_parser.add_argument("--output", type=Path, required=True, help="new directory for trace, scorecard, and artifacts")
    run_parser.add_argument("--baseline", type=Path, help="optional baseline scorecard for regression comparison")
    canonicalize_parser = subparsers.add_parser("canonicalize", help="write the stable projection of a trace")
    canonicalize_parser.add_argument("--trace", type=Path, required=True, help="trace.jsonl to canonicalize")
    canonicalize_parser.add_argument("--output", type=Path, required=True, help="JSON output file")
    compare_parser = subparsers.add_parser("compare", help="compare two machine-readable scorecards")
    compare_parser.add_argument("--baseline", type=Path, required=True, help="baseline scorecard JSON")
    compare_parser.add_argument("--current", type=Path, required=True, help="current scorecard JSON")
    compare_parser.add_argument("--output", type=Path, help="optional JSON output file")
    args = parser.parse_args(argv)

    if args.command == "run":
        baseline = _load_json(args.baseline) if args.baseline is not None else None
        result = run_case_sync(args.case, args.output, baseline_scorecard=baseline)
        print(json.dumps(result.scorecard, ensure_ascii=False, sort_keys=True))
        comparison = result.scorecard.get("comparison")
        return 0 if result.scorecard["passed"] and (comparison is None or comparison.get("passed")) else 1
    if args.command == "canonicalize":
        args.output.write_text(canonical_json(load_trace(args.trace)), encoding="utf-8")
        return 0
    comparison = compare_scorecards(_load_json(args.baseline), _load_json(args.current))
    payload = json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if comparison["passed"] else 1


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"scorecard must be a JSON object: {path}")
    return value
