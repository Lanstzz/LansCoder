"""AtCoder online-judge benchmark runner for FirstCoder."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.eval.tasks import CodingTask, CodingTaskResult
from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import PermissionAction, PermissionDecision, PermissionDecisionKind, PermissionMode
from firstcoder.providers.factory import create_provider
from firstcoder.tools.builtin import create_builtin_registry


DEFAULT_SOLUTION = "main.py"
DEFAULT_PYTHON = "python3"
VERDICTS = ("AC", "WA", "TLE", "MLE", "RE", "CE", "OLE", "IE", "WJ", "Judging")
SUBMISSION_URL_PATTERN = re.compile(r"/submissions/(\d+)")
SUBMISSION_ID_PATTERN = re.compile(r"\bID\s+(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AtCoderTask:
    contest: str
    task: str
    url: str | None = None

    def __post_init__(self) -> None:
        if self.url is None:
            object.__setattr__(self, "url", f"https://atcoder.jp/contests/{self.contest}/tasks/{self.task}")

    @property
    def instance_id(self) -> str:
        return f"{self.contest}__{self.task}"


@dataclass(frozen=True, slots=True)
class OjSubmissionResult:
    verdict: str | None
    submission_id: str | None
    raw_output: str


def load_tasks_jsonl(path: str | Path) -> list[AtCoderTask]:
    tasks: list[AtCoderTask] = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            data = json.loads(line)
            tasks.append(
                AtCoderTask(
                    contest=str(data["contest"]),
                    task=str(data["task"]),
                    url=str(data["url"]) if data.get("url") else None,
                )
            )
    return tasks


def build_acc_new_command(task: AtCoderTask, workdir: str | Path) -> list[str]:
    return ["acc", "new", task.contest, "--choice", "all"]


def build_oj_test_command(task_dir: str | Path, solution_name: str = DEFAULT_SOLUTION, python_command: str = DEFAULT_PYTHON) -> list[str]:
    return ["oj", "test", "-c", f"{python_command} {solution_name}"]


def build_oj_submit_command(task: AtCoderTask, task_dir: str | Path, solution_name: str = DEFAULT_SOLUTION) -> list[str]:
    return ["oj", "submit", str(task.url), solution_name, "--yes"]


def run_command(command: list[str], *, cwd: str | Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=Path(cwd),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        tool = command[0] if command else str(exc)
        hint = "Install atcoder-cli and online-judge-tools: pip install online-judge-tools atcoder-cli"
        raise RuntimeError(f"Missing required CLI tool: {tool}. {hint}") from exc
    except subprocess.CalledProcessError as exc:
        output = exc.stdout or ""
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{output}") from exc


def parse_oj_submission_output(output: str) -> OjSubmissionResult:
    verdict = _find_verdict(output)
    submission_id = _find_submission_id(output)
    return OjSubmissionResult(verdict=verdict, submission_id=submission_id, raw_output=output)


def write_summary_json(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def task_directory(workdir: str | Path, task: AtCoderTask) -> Path:
    root = Path(workdir)
    suffix = task.task.removeprefix(f"{task.contest}_")
    candidate = root / task.contest / suffix
    if candidate.exists():
        return candidate
    full = root / task.contest / task.task
    if full.exists():
        return full
    return candidate


def run_tasks(
    *,
    tasks: list[AtCoderTask],
    workdir: str | Path,
    submit: bool,
    max_tasks: int | None = None,
    provider_name: str | None = None,
    model_name: str = "firstcoder-atcoder",
    session_root: str | Path = ".firstcoder-atcoder",
    solution_name: str = DEFAULT_SOLUTION,
    python_command: str = DEFAULT_PYTHON,
) -> list[dict[str, Any]]:
    selected = tasks[:max_tasks] if max_tasks is not None else tasks
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    adapter = AtCoderAgentAdapter(
        model_name_or_path=model_name,
        provider_name=provider_name,
        session_root=session_root,
    )
    for task in selected:
        row = run_one_task(
            task=task,
            workdir=workdir_path,
            adapter=adapter,
            submit=submit,
            solution_name=solution_name,
            python_command=python_command,
        )
        results.append(row)
    return results


def run_one_task(
    *,
    task: AtCoderTask,
    workdir: Path,
    adapter: AtCoderAgentAdapter,
    submit: bool,
    solution_name: str,
    python_command: str,
) -> dict[str, Any]:
    task_dir = task_directory(workdir, task)
    if not task_dir.exists():
        run_command(build_acc_new_command(task, workdir), cwd=workdir)
        task_dir = task_directory(workdir, task)
    problem_statement = _build_problem_statement(task, solution_name)
    started_at = time.time()
    result = adapter.run_task(
        CodingTask(
            instance_id=task.instance_id,
            repo_path=task_dir,
            problem_statement=problem_statement,
            metadata={"contest": task.contest, "task": task.task, "url": task.url},
        )
    )
    sample_output = run_command(build_oj_test_command(task_dir, solution_name, python_command), cwd=task_dir).stdout
    submission: OjSubmissionResult | None = None
    if submit:
        submit_output = run_command(build_oj_submit_command(task, task_dir, solution_name), cwd=task_dir).stdout
        submission = parse_oj_submission_output(submit_output)
    return {
        "contest": task.contest,
        "task": task.task,
        "url": task.url,
        "task_dir": str(task_dir),
        "solution": solution_name,
        "samples_passed": True,
        "submitted": submit,
        "verdict": submission.verdict if submission else None,
        "submission_id": submission.submission_id if submission else None,
        "transcript_path": str(result.transcript_path) if result.transcript_path else None,
        "raw_response": result.raw_response,
        "elapsed_seconds": round(time.time() - started_at, 3),
        "sample_output": sample_output,
        "submit_output": submission.raw_output if submission else "",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run FirstCoder on AtCoder tasks and submit solutions for real verdicts.")
    parser.add_argument("--tasks", required=True, help="JSONL file with contest/task/url rows.")
    parser.add_argument("--workdir", required=True, help="Directory where acc creates contest task folders.")
    parser.add_argument("--summary-out", default="runs/atcoder-summary.json", help="JSON summary path.")
    parser.add_argument("--submit", action="store_true", help="Submit solutions to AtCoder with oj submit.")
    parser.add_argument("--max-tasks", type=_positive_int, default=None, help="Limit number of tasks.")
    parser.add_argument("--provider", default=None, help="FirstCoder provider name. Defaults to app config.")
    parser.add_argument("--model-name", default="firstcoder-atcoder", help="Model name recorded in benchmark sessions.")
    parser.add_argument("--session-root", default=".firstcoder-atcoder", help="Directory for FirstCoder benchmark sessions.")
    parser.add_argument("--solution", default=DEFAULT_SOLUTION, help="Solution filename to create and submit.")
    parser.add_argument("--python-command", default=DEFAULT_PYTHON, help="Python command used by oj test.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = run_tasks(
            tasks=load_tasks_jsonl(args.tasks),
            workdir=args.workdir,
            submit=args.submit,
            max_tasks=args.max_tasks,
            provider_name=args.provider,
            model_name=args.model_name,
            session_root=args.session_root,
            solution_name=args.solution,
            python_command=args.python_command,
        )
        write_summary_json(args.summary_out, rows)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote AtCoder benchmark summary: {args.summary_out}")
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _find_verdict(output: str) -> str | None:
    for verdict in VERDICTS:
        if re.search(rf"\b{re.escape(verdict)}\b", output):
            return verdict
    return None


def _find_submission_id(output: str) -> str | None:
    url_match = SUBMISSION_URL_PATTERN.search(output)
    if url_match:
        return url_match.group(1)
    id_match = SUBMISSION_ID_PATTERN.search(output)
    if id_match:
        return id_match.group(1)
    return None


def _build_problem_statement(task: AtCoderTask, solution_name: str) -> str:
    return (
        "Solve this AtCoder task.\n"
        f"Contest: {task.contest}\n"
        f"Task: {task.task}\n"
        f"URL: {task.url}\n\n"
        f"Use the official samples already downloaded in this directory. Write the final Python solution to {solution_name}. "
        "You may inspect files and run local sample tests, but do not submit. The benchmark runner submits after samples pass."
    )


class AtCoderAgentAdapter:
    def __init__(
        self,
        *,
        model_name_or_path: str,
        provider_name: str | None,
        session_root: str | Path,
    ) -> None:
        self.model_name_or_path = model_name_or_path
        self.provider_name = provider_name
        self.session_root = Path(session_root)

    def run_task(self, task: CodingTask) -> CodingTaskResult:
        session_root = self._session_root_for_task(task)
        session_root.mkdir(parents=True, exist_ok=True)
        loop = self._create_loop(task, session_root)
        response = loop.run_user_turn(_build_task_prompt(task))
        solution_path = task.repo_path / str(task.metadata.get("solution", DEFAULT_SOLUTION))
        solution_text = solution_path.read_text(encoding="utf-8") if solution_path.exists() else ""
        return CodingTaskResult(
            instance_id=task.instance_id,
            model_name_or_path=self.model_name_or_path,
            model_patch=solution_text,
            transcript_path=session_root / "sessions" / f"{_session_dir_name(task.instance_id)}.jsonl",
            raw_response=response.content,
        )

    def _session_root_for_task(self, task: CodingTask) -> Path:
        root = self.session_root
        if not root.is_absolute():
            root = task.repo_path.resolve().parent / root
        return (root / _session_dir_name(task.instance_id)).resolve()

    def _create_loop(self, task: CodingTask, session_root: Path) -> AgentLoop:
        registry = create_builtin_registry(
            task.repo_path,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=False,
        )
        permission_manager = PermissionManager(
            policy=AtCoderBenchmarkPermissionPolicy(task.repo_path),
            grants=PermissionGrantStore(),
            mode=PermissionMode.AGGRESSIVE,
        )
        store = JsonlSessionStore(session_root)
        tools = registry.tools()
        session = AgentSession.from_project(
            store=store,
            session_id=_session_dir_name(task.instance_id),
            project_root=task.repo_path,
            tools=tools,
            permission_manager=permission_manager,
        )
        return AgentLoop(
            session=session,
            provider=create_provider(self.provider_name),
            tools=tools,
            limits=AgentLoopLimits.swe_lite(),
        )


class AtCoderBenchmarkPermissionPolicy(DefaultPermissionPolicy):
    def decide(self, request, *, mode: PermissionMode) -> PermissionDecision:
        if request.action in {PermissionAction.WRITE_PATH, PermissionAction.EXECUTE_SHELL}:
            target = self._resolve_path(request.target, cwd=request.cwd)
            if request.action == PermissionAction.EXECUTE_SHELL and self._request_cwd_inside_root(request):
                return PermissionDecision(
                    kind=PermissionDecisionKind.ALLOW,
                    reason="AtCoder benchmark allows local commands inside the task directory.",
                )
            if request.action == PermissionAction.WRITE_PATH and self._is_inside_project(target) and not self._is_sensitive_path(target):
                return PermissionDecision(
                    kind=PermissionDecisionKind.ALLOW,
                    reason="AtCoder benchmark allows local writes inside the task directory.",
                )
        return super().decide(request, mode=mode)


def _build_task_prompt(task: CodingTask) -> str:
    return (
        "You are running inside an AtCoder benchmark task.\n"
        f"Instance: {task.instance_id}\n\n"
        "Problem statement:\n"
        f"{task.problem_statement.strip()}\n\n"
        "Return by editing files in the current directory. Keep the solution minimal and compatible with Python 3."
    )


def _session_dir_name(instance_id: str) -> str:
    safe = instance_id.replace("/", "_").replace("\\", "_").replace(":", "_")
    while ".." in safe:
        safe = safe.replace("..", "__")
    return safe or "instance"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
