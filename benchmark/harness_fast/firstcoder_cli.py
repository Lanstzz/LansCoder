"""CLI shim used by harness-bench-fast `run-cli`.

The benchmark invokes external agents from inside each task workspace and
passes the prompt as the final positional argument. This shim adapts that
shape to FirstCoder's in-process benchmark adapter.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from firstcoder.eval.adapter import FirstCoderCodingAgentAdapter
from firstcoder.eval.tasks import CodingTask


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run FirstCoder on the current harness-bench workspace.")
    parser.add_argument("prompt", help="Benchmark task prompt.")
    parser.add_argument("--provider", default=None, help="FirstCoder provider override.")
    parser.add_argument("--model-name", default="firstcoder-harness-bench-fast")
    parser.add_argument("--session-root", default=".firstcoder-harness")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path.cwd()
    _ensure_git_repo(workspace)
    adapter = FirstCoderCodingAgentAdapter(
        model_name_or_path=args.model_name,
        provider_name=args.provider,
        session_root=args.session_root,
    )
    result = adapter.run_task(
        CodingTask(
            instance_id=workspace.name,
            repo_path=workspace,
            problem_statement=args.prompt,
            metadata={"benchmark": "harness-bench-fast"},
        )
    )
    if result.raw_response:
        print(result.raw_response)
    return 0


def _ensure_git_repo(workspace: Path) -> None:
    if (workspace / ".git").exists():
        return
    _git(["init"], workspace)
    _git(["config", "user.email", "benchmark@example.com"], workspace)
    _git(["config", "user.name", "Benchmark"], workspace)
    (workspace / ".gitignore").write_text(".firstcoder-harness/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n", encoding="utf-8")
    _git(["add", "-A"], workspace)
    _git(["commit", "-m", "initial harness task"], workspace)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
