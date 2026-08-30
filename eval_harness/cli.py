"""Command-line entry point for offline evaluation runs and golden comparison data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval_harness.replay.runner import run_case_sync
from eval_harness.trace.canonicalize import canonical_json, load_trace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_harness", description="Offline-first LansCoder evaluation harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run one offline interaction replay case")
    run_parser.add_argument("--case", type=Path, required=True, help="portable JSON case manifest")
    run_parser.add_argument("--output", type=Path, required=True, help="new directory for trace, scorecard, and artifacts")
    canonicalize_parser = subparsers.add_parser("canonicalize", help="write the stable projection of a trace")
    canonicalize_parser.add_argument("--trace", type=Path, required=True, help="trace.jsonl to canonicalize")
    canonicalize_parser.add_argument("--output", type=Path, required=True, help="JSON output file")
    args = parser.parse_args(argv)

    if args.command == "run":
        result = run_case_sync(args.case, args.output)
        print(json.dumps(result.scorecard, ensure_ascii=False, sort_keys=True))
        return 0 if result.scorecard["passed"] else 1
    args.output.write_text(canonical_json(load_trace(args.trace)), encoding="utf-8")
    return 0
