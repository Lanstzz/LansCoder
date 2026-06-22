# SWE-bench Lite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is written for TDD: write the failing test first, implement the smallest useful slice, run focused tests, request a review subagent, then commit before continuing.

**Goal:** Make FirstCoder produce official SWE-bench Lite-compatible prediction JSONL files, then optionally run the official Docker evaluation harness against those predictions.

**Architecture:** Add a small benchmark/eval layer outside the core provider stack. SWE-bench instances are converted into generic coding tasks, a `FirstCoderCodingAgentAdapter` runs `AgentSession + AgentLoop` inside a checked-out task repository, and a patch collector writes official `predictions.jsonl` lines with `instance_id`, `model_name_or_path`, and `model_patch`.

**Tech Stack:** Python standard library, existing FirstCoder `AgentSession`, `AgentLoop`, `JsonlSessionStore`, `create_provider`, built-in tools, `pytest`, `git`, optional `datasets` and optional official `swebench` package.

---

## External Contract

The official SWE-bench evaluation guide says SWE-bench Lite can be evaluated with:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path <path_to_predictions> \
  --max_workers 8 \
  --run_id my_first_evaluation
```

The prediction file must be JSONL, one object per line:

```json
{"instance_id":"sympy__sympy-20590","model_name_or_path":"firstcoder","model_patch":"diff --git ..."}
```

Reference: [SWE-bench Evaluation Guide](https://www.swebench.com/SWE-bench/guides/evaluation/).

## Scope

This plan intentionally builds the lightest useful SWE-bench Lite path first:

- Generate official prediction JSONL from local JSONL SWE-bench instance data.
- Run FirstCoder as an agent, not as a `ChatProvider`.
- Collect `git diff --binary` from each task repo as `model_patch`.
- Add a CLI module under `firstcoder.eval.swebench`.
- Keep official Docker evaluation as a wrapper command that consumes generated predictions.

This first version does not implement automatic Docker repo preparation from scratch. It expects each instance repo to already exist at a local path, and later tasks can add HuggingFace dataset loading and automated checkout.

## File Structure

- Create `firstcoder/eval/__init__.py`: package marker and public exports.
- Create `firstcoder/eval/tasks.py`: generic benchmark task/result dataclasses.
- Create `firstcoder/eval/patch.py`: git diff collection and repo cleanliness helpers.
- Create `firstcoder/eval/adapter.py`: `CodingAgentAdapter` protocol and `FirstCoderCodingAgentAdapter`.
- Create `firstcoder/eval/swebench.py`: SWE-bench JSONL parsing, prompt building, prediction writing, CLI entrypoint.
- Create `tests/test_eval_tasks.py`: task/result serialization tests.
- Create `tests/test_eval_patch.py`: git diff helper tests.
- Create `tests/test_eval_adapter.py`: adapter tests with fake provider and temporary repo.
- Create `tests/test_eval_swebench.py`: SWE-bench runner and CLI tests.
- Modify `requirements.txt`: add optional comments only if needed; avoid mandatory benchmark dependencies in MVP.

## Implementation Notes

- FirstCoder must remain the agent boundary. Do not wrap FirstCoder as a `ChatProvider`.
- Use existing provider creation from `firstcoder.providers.factory.create_provider`.
- Use `AgentSession.from_project(...)`, `AgentLoop(...)`, and `JsonlSessionStore(...)`.
- SWE-bench Lite runs should use `AgentLoopLimits.swe_lite()` so the agent has enough budget for real debugging while still stopping on provider-call and wall-clock limits.
- Use `create_builtin_registry(..., include_mutation_tools=True, include_execution_tools=True)` for benchmark runs.
- For benchmark automation, permissions should be non-interactive. Configure the permission manager so file edits and shell execution are allowed for the task repo, or use an adapter option that injects permissive policy for the temporary benchmark session.
- Keep benchmark session logs outside the target repo, for example under `<work-dir>/sessions/<instance_id>/`, so generated session state does not contaminate `git diff`.
- Patch collection must ignore untracked FirstCoder session files by keeping sessions out of the repo. If untracked task files are created intentionally, include them in the patch by staging intent or by using a helper that adds untracked files to a temporary index before diffing.
- The CLI should default to dry, small runs: `--max-instances 1`, explicit `--instances`, explicit `--repos-root`, explicit `--out`.

---

### Task 1: Generic Eval Task Models

**Files:**
- Create: `firstcoder/eval/__init__.py`
- Create: `firstcoder/eval/tasks.py`
- Test: `tests/test_eval_tasks.py`

- [ ] **Step 1: Write the failing tests**

Add `tests/test_eval_tasks.py`:

```python
from pathlib import Path

from firstcoder.eval.tasks import CodingTask, CodingTaskResult


def test_coding_task_exposes_prompt_inputs(tmp_path: Path):
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=tmp_path,
        problem_statement="Fix sympify error handling.",
        base_commit="abc123",
        metadata={"repo": "sympy/sympy"},
    )

    assert task.instance_id == "sympy__sympy-20590"
    assert task.repo_path == tmp_path
    assert task.metadata["repo"] == "sympy/sympy"


def test_coding_task_result_serializes_to_swebench_prediction():
    result = CodingTaskResult(
        instance_id="sympy__sympy-20590",
        model_name_or_path="firstcoder",
        model_patch="diff --git a/a.py b/a.py\n",
        transcript_path=Path("/tmp/session.jsonl"),
    )

    assert result.to_prediction_dict() == {
        "instance_id": "sympy__sympy-20590",
        "model_name_or_path": "firstcoder",
        "model_patch": "diff --git a/a.py b/a.py\n",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_eval_tasks.py -q
```

Expected: FAIL because `firstcoder.eval.tasks` does not exist.

- [ ] **Step 3: Implement minimal task models**

Create `firstcoder/eval/__init__.py`:

```python
"""Evaluation adapters for external coding benchmarks."""
```

Create `firstcoder/eval/tasks.py`:

```python
"""Generic coding benchmark task models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CodingTask:
    """A repository-level coding task for a benchmark adapter."""

    instance_id: str
    repo_path: Path
    problem_statement: str
    base_commit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodingTaskResult:
    """The patch and trace produced for one benchmark task."""

    instance_id: str
    model_name_or_path: str
    model_patch: str
    transcript_path: Path | None = None
    raw_response: str = ""

    def to_prediction_dict(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch,
        }
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_eval_tasks.py -q
```

Expected: PASS.

- [ ] **Step 5: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 1 for the SWE-bench Lite plan. Focus on whether the generic task/result models are minimal, stable, and compatible with official SWE-bench prediction JSONL. Check tests and implementation only for this task.
```

If review passes:

```bash
git add firstcoder/eval/__init__.py firstcoder/eval/tasks.py tests/test_eval_tasks.py
git commit -m "feat(eval): add coding task models"
```

---

### Task 2: Git Patch Collection

**Files:**
- Create: `firstcoder/eval/patch.py`
- Test: `tests/test_eval_patch.py`

- [ ] **Step 1: Write the failing tests**

Add `tests/test_eval_patch.py`:

```python
import subprocess
from pathlib import Path

from firstcoder.eval.patch import collect_git_diff, ensure_clean_repo


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test User"], repo)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run(["git", "add", "module.py"], repo)
    run(["git", "commit", "-m", "init"], repo)
    return repo


def test_ensure_clean_repo_accepts_clean_repo(tmp_path: Path):
    repo = init_repo(tmp_path)

    ensure_clean_repo(repo)


def test_ensure_clean_repo_rejects_dirty_repo(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    try:
        ensure_clean_repo(repo)
    except RuntimeError as exc:
        assert "dirty" in str(exc)
    else:
        raise AssertionError("Expected dirty repo error")


def test_collect_git_diff_includes_tracked_modifications(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    diff = collect_git_diff(repo)

    assert "diff --git a/module.py b/module.py" in diff
    assert "-VALUE = 1" in diff
    assert "+VALUE = 2" in diff
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_eval_patch.py -q
```

Expected: FAIL because `firstcoder.eval.patch` does not exist.

- [ ] **Step 3: Implement git helpers**

Create `firstcoder/eval/patch.py`:

```python
"""Git helpers for benchmark patch generation."""

from __future__ import annotations

import subprocess
from pathlib import Path


def ensure_clean_repo(repo_path: str | Path) -> None:
    repo = Path(repo_path)
    result = _git(["status", "--porcelain"], repo)
    if result.stdout.strip():
        raise RuntimeError(f"Repository is dirty before benchmark run: {repo}")


def collect_git_diff(repo_path: str | Path) -> str:
    repo = Path(repo_path)
    return _git(["diff", "--binary"], repo).stdout


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_eval_patch.py -q
```

Expected: PASS.

- [ ] **Step 5: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 2 for the SWE-bench Lite plan. Focus on git diff correctness, whether dirty repo detection is safe, and whether the helper avoids contaminating benchmark patches.
```

If review passes:

```bash
git add firstcoder/eval/patch.py tests/test_eval_patch.py
git commit -m "feat(eval): collect benchmark git patches"
```

---

### Task 3: FirstCoder Coding Agent Adapter

**Files:**
- Create: `firstcoder/eval/adapter.py`
- Test: `tests/test_eval_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add `tests/test_eval_adapter.py`:

```python
from pathlib import Path

from firstcoder.eval.adapter import FirstCoderCodingAgentAdapter
from firstcoder.eval.tasks import CodingTask
from firstcoder.providers.types import ChatResponse


class FakeLoop:
    def __init__(self):
        self.messages: list[str] = []

    def run_user_turn(self, content: str) -> ChatResponse:
        self.messages.append(content)
        return ChatResponse(
            provider="fake",
            model="fake-model",
            content="done",
            finish_reason="stop",
        )


def test_adapter_builds_benchmark_prompt(tmp_path: Path):
    loop = FakeLoop()
    adapter = FirstCoderCodingAgentAdapter(
        model_name_or_path="firstcoder-test",
        loop_factory=lambda task, session_root: loop,
        session_root=tmp_path / "sessions",
    )
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=tmp_path,
        problem_statement="Fix the issue.",
        base_commit="abc123",
    )

    result = adapter.run_task(task)

    assert result.instance_id == "sympy__sympy-20590"
    assert result.model_name_or_path == "firstcoder-test"
    assert "Fix the issue." in loop.messages[0]
    assert "Return by editing files" in loop.messages[0]
    assert result.raw_response == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_eval_adapter.py -q
```

Expected: FAIL because `FirstCoderCodingAgentAdapter` does not exist.

- [ ] **Step 3: Implement adapter seam**

Create `firstcoder/eval/adapter.py`:

```python
"""Coding-agent adapters used by benchmark runners."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.eval.patch import collect_git_diff
from firstcoder.eval.tasks import CodingTask, CodingTaskResult
from firstcoder.permissions.grants import MemoryPermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.types import PermissionMode
from firstcoder.providers.factory import create_provider
from firstcoder.tools.builtin import create_builtin_registry


class CodingAgentAdapter(Protocol):
    def run_task(self, task: CodingTask) -> CodingTaskResult:
        ...


LoopFactory = Callable[[CodingTask, Path], AgentLoop]


class FirstCoderCodingAgentAdapter:
    """Runs FirstCoder against one repository-level coding task."""

    def __init__(
        self,
        *,
        model_name_or_path: str = "firstcoder",
        provider_name: str | None = None,
        session_root: str | Path = ".firstcoder-eval",
        loop_factory: LoopFactory | None = None,
    ) -> None:
        self.model_name_or_path = model_name_or_path
        self.provider_name = provider_name
        self.session_root = Path(session_root)
        self.loop_factory = loop_factory or self._create_loop

    def run_task(self, task: CodingTask) -> CodingTaskResult:
        session_root = self.session_root / task.instance_id
        session_root.mkdir(parents=True, exist_ok=True)
        loop = self.loop_factory(task, session_root)
        response = loop.run_user_turn(_build_task_prompt(task))
        return CodingTaskResult(
            instance_id=task.instance_id,
            model_name_or_path=self.model_name_or_path,
            model_patch=collect_git_diff(task.repo_path),
            transcript_path=session_root / "sessions" / f"{task.instance_id}.jsonl",
            raw_response=response.content,
        )

    def _create_loop(self, task: CodingTask, session_root: Path) -> AgentLoop:
        registry = create_builtin_registry(
            task.repo_path,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=False,
        )
        permission_manager = PermissionManager(
            mode=PermissionMode.BYPASS,
            policy=None,
            grants=MemoryPermissionGrantStore(),
        )
        store = JsonlSessionStore(session_root)
        session = AgentSession.from_project(
            store=store,
            session_id=task.instance_id,
            project_root=task.repo_path,
            tools=list(registry),
            permission_manager=permission_manager,
        )
        return AgentLoop(
            session=session,
            provider=create_provider(self.provider_name),
            tools=list(registry),
            limits=AgentLoopLimits.swe_lite(),
        )


def _build_task_prompt(task: CodingTask) -> str:
    base_commit = task.base_commit or "unknown"
    return (
        "You are running inside a SWE-bench style benchmark task.\n"
        f"Instance: {task.instance_id}\n"
        f"Base commit: {base_commit}\n\n"
        "Problem statement:\n"
        f"{task.problem_statement.strip()}\n\n"
        "Return by editing files in the repository. Do not write a final patch manually. "
        "Use tests when useful, keep changes minimal, and leave the repository with the fix applied."
    )
```

- [ ] **Step 4: Check permission imports before running**

Run:

```bash
grep -R "class MemoryPermissionGrantStore\\|PermissionMode.BYPASS" -n firstcoder tests
```

Expected: both symbols exist. If either name differs, update `adapter.py` to match the existing permission API before running tests.

- [ ] **Step 5: Run focused tests**

Run:

```bash
pytest tests/test_eval_adapter.py -q
```

Expected: PASS.

- [ ] **Step 6: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 3 for the SWE-bench Lite plan. Focus on whether FirstCoder is invoked as an agent through AgentSession + AgentLoop, whether session files stay outside the target repo, and whether benchmark permissions are explicit and non-interactive.
```

If review passes:

```bash
git add firstcoder/eval/adapter.py tests/test_eval_adapter.py
git commit -m "feat(eval): add firstcoder coding adapter"
```

---

### Task 4: SWE-bench JSONL Runner

**Files:**
- Create: `firstcoder/eval/swebench.py`
- Test: `tests/test_eval_swebench.py`

- [ ] **Step 1: Write the failing tests**

Add `tests/test_eval_swebench.py`:

```python
import json
from pathlib import Path

from firstcoder.eval.swebench import (
    SwebenchInstance,
    load_instances_jsonl,
    repo_path_for_instance,
    write_predictions_jsonl,
)
from firstcoder.eval.tasks import CodingTaskResult


def test_load_instances_jsonl_reads_minimal_swebench_fields(tmp_path: Path):
    path = tmp_path / "instances.jsonl"
    path.write_text(
        json.dumps(
            {
                "instance_id": "sympy__sympy-20590",
                "repo": "sympy/sympy",
                "base_commit": "abc123",
                "problem_statement": "Fix it.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    instances = load_instances_jsonl(path)

    assert instances == [
        SwebenchInstance(
            instance_id="sympy__sympy-20590",
            repo="sympy/sympy",
            base_commit="abc123",
            problem_statement="Fix it.",
        )
    ]


def test_repo_path_for_instance_uses_sanitized_instance_id(tmp_path: Path):
    instance = SwebenchInstance(
        instance_id="sympy__sympy-20590",
        repo="sympy/sympy",
        base_commit="abc123",
        problem_statement="Fix it.",
    )

    assert repo_path_for_instance(tmp_path, instance) == tmp_path / "sympy__sympy-20590"


def test_write_predictions_jsonl(tmp_path: Path):
    out = tmp_path / "predictions.jsonl"
    write_predictions_jsonl(
        out,
        [
            CodingTaskResult(
                instance_id="sympy__sympy-20590",
                model_name_or_path="firstcoder",
                model_patch="diff --git a/a.py b/a.py\n",
            )
        ],
    )

    assert out.read_text(encoding="utf-8") == (
        '{"instance_id":"sympy__sympy-20590","model_name_or_path":"firstcoder",'
        '"model_patch":"diff --git a/a.py b/a.py\\n"}\n'
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_eval_swebench.py -q
```

Expected: FAIL because `firstcoder.eval.swebench` does not exist.

- [ ] **Step 3: Implement SWE-bench helpers**

Create `firstcoder/eval/swebench.py`:

```python
"""SWE-bench Lite prediction generation for FirstCoder."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from firstcoder.eval.adapter import CodingAgentAdapter, FirstCoderCodingAgentAdapter
from firstcoder.eval.patch import ensure_clean_repo
from firstcoder.eval.tasks import CodingTask, CodingTaskResult


@dataclass(frozen=True, slots=True)
class SwebenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str


def load_instances_jsonl(path: str | Path) -> list[SwebenchInstance]:
    instances: list[SwebenchInstance] = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            data = json.loads(line)
            instances.append(
                SwebenchInstance(
                    instance_id=str(data["instance_id"]),
                    repo=str(data["repo"]),
                    base_commit=str(data["base_commit"]),
                    problem_statement=str(data["problem_statement"]),
                )
            )
    return instances


def repo_path_for_instance(repos_root: str | Path, instance: SwebenchInstance) -> Path:
    return Path(repos_root) / instance.instance_id


def write_predictions_jsonl(path: str | Path, results: Iterable[CodingTaskResult]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as file:
        for result in results:
            file.write(json.dumps(result.to_prediction_dict(), ensure_ascii=False, separators=(",", ":")))
            file.write("\n")


def run_instances(
    *,
    instances: list[SwebenchInstance],
    repos_root: str | Path,
    adapter: CodingAgentAdapter,
    max_instances: int | None = None,
) -> list[CodingTaskResult]:
    selected = instances[:max_instances] if max_instances is not None else instances
    results: list[CodingTaskResult] = []
    for instance in selected:
        repo_path = repo_path_for_instance(repos_root, instance)
        ensure_clean_repo(repo_path)
        task = CodingTask(
            instance_id=instance.instance_id,
            repo_path=repo_path,
            problem_statement=instance.problem_statement,
            base_commit=instance.base_commit,
            metadata={"repo": instance.repo},
        )
        results.append(adapter.run_task(task))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate SWE-bench Lite predictions with FirstCoder.")
    parser.add_argument("--instances", required=True, help="Path to SWE-bench style instances JSONL.")
    parser.add_argument("--repos-root", required=True, help="Directory containing one repo per instance_id.")
    parser.add_argument("--out", required=True, help="Output predictions JSONL path.")
    parser.add_argument("--provider", default=None, help="FirstCoder provider name. Defaults to app config.")
    parser.add_argument("--model-name", default="firstcoder", help="Value for model_name_or_path.")
    parser.add_argument("--session-root", default=".firstcoder-eval", help="Directory for benchmark session logs.")
    parser.add_argument("--max-instances", type=int, default=1, help="Maximum instances to run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapter = FirstCoderCodingAgentAdapter(
        model_name_or_path=args.model_name,
        provider_name=args.provider,
        session_root=args.session_root,
    )
    results = run_instances(
        instances=load_instances_jsonl(args.instances),
        repos_root=args.repos_root,
        adapter=adapter,
        max_instances=args.max_instances,
    )
    write_predictions_jsonl(args.out, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_eval_swebench.py -q
```

Expected: PASS.

- [ ] **Step 5: Add CLI behavior tests**

Append to `tests/test_eval_swebench.py`:

```python
from firstcoder.eval.swebench import build_parser


def test_parser_defaults_to_one_instance():
    args = build_parser().parse_args(
        [
            "--instances",
            "instances.jsonl",
            "--repos-root",
            "repos",
            "--out",
            "predictions.jsonl",
        ]
    )

    assert args.max_instances == 1
    assert args.model_name == "firstcoder"
```

- [ ] **Step 6: Run focused tests again**

Run:

```bash
pytest tests/test_eval_swebench.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 4 for the SWE-bench Lite plan. Focus on official prediction JSONL compatibility, CLI ergonomics, deterministic output, and whether the runner avoids hidden dataset/download assumptions.
```

If review passes:

```bash
git add firstcoder/eval/swebench.py tests/test_eval_swebench.py
git commit -m "feat(eval): generate swebench predictions"
```

---

### Task 5: Untracked File Patch Support

**Files:**
- Modify: `firstcoder/eval/patch.py`
- Modify: `tests/test_eval_patch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_patch.py`:

```python
def test_collect_git_diff_can_include_untracked_files(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "new_module.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")

    diff = collect_git_diff(repo, include_untracked=True)

    assert "diff --git a/new_module.py b/new_module.py" in diff
    assert "+NEW_VALUE = 3" in diff
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_eval_patch.py::test_collect_git_diff_can_include_untracked_files -q
```

Expected: FAIL because `collect_git_diff()` does not accept `include_untracked`.

- [ ] **Step 3: Implement untracked support with a temporary index**

Update `firstcoder/eval/patch.py`:

```python
"""Git helpers for benchmark patch generation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def ensure_clean_repo(repo_path: str | Path) -> None:
    repo = Path(repo_path)
    result = _git(["status", "--porcelain"], repo)
    if result.stdout.strip():
        raise RuntimeError(f"Repository is dirty before benchmark run: {repo}")


def collect_git_diff(repo_path: str | Path, *, include_untracked: bool = False) -> str:
    repo = Path(repo_path)
    if not include_untracked:
        return _git(["diff", "--binary"], repo).stdout
    return _collect_diff_with_untracked(repo)


def _collect_diff_with_untracked(repo: Path) -> str:
    with tempfile.NamedTemporaryFile(prefix="firstcoder-index-") as index:
        head_tree = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        env = {"GIT_INDEX_FILE": index.name}
        _git(["read-tree", head_tree], repo, env=env)
        _git(["add", "-A"], repo, env=env)
        return _git(["diff", "--cached", "--binary"], repo, env=env).stdout


def _git(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=command_env,
        check=True,
        text=True,
        capture_output=True,
    )
```

- [ ] **Step 4: Run patch tests**

Run:

```bash
pytest tests/test_eval_patch.py -q
```

Expected: PASS.

- [ ] **Step 5: Update adapter to include untracked benchmark changes**

Modify `firstcoder/eval/adapter.py` so `run_task()` uses:

```python
model_patch=collect_git_diff(task.repo_path, include_untracked=True),
```

- [ ] **Step 6: Run adapter and patch tests**

Run:

```bash
pytest tests/test_eval_patch.py tests/test_eval_adapter.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 5 for the SWE-bench Lite plan. Focus on whether untracked files are included without mutating the real git index, and whether binary/tracked diffs still work.
```

If review passes:

```bash
git add firstcoder/eval/patch.py firstcoder/eval/adapter.py tests/test_eval_patch.py
git commit -m "feat(eval): include untracked benchmark files in patches"
```

---

### Task 6: Official Harness Command Helper

**Files:**
- Modify: `firstcoder/eval/swebench.py`
- Modify: `tests/test_eval_swebench.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_swebench.py`:

```python
from firstcoder.eval.swebench import build_harness_command


def test_build_harness_command_for_swebench_lite():
    command = build_harness_command(
        predictions_path=Path("runs/firstcoder_predictions.jsonl"),
        run_id="firstcoder-smoke",
        max_workers=2,
    )

    assert command == [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "princeton-nlp/SWE-bench_Lite",
        "--predictions_path",
        "runs/firstcoder_predictions.jsonl",
        "--max_workers",
        "2",
        "--run_id",
        "firstcoder-smoke",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_eval_swebench.py::test_build_harness_command_for_swebench_lite -q
```

Expected: FAIL because `build_harness_command` does not exist.

- [ ] **Step 3: Implement command helper**

Add to `firstcoder/eval/swebench.py`:

```python
def build_harness_command(
    *,
    predictions_path: str | Path,
    run_id: str,
    max_workers: int = 1,
    dataset_name: str = "princeton-nlp/SWE-bench_Lite",
) -> list[str]:
    return [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_eval_swebench.py -q
```

Expected: PASS.

- [ ] **Step 5: Add CLI print option for the official command**

Modify `build_parser()` to include:

```python
parser.add_argument("--print-harness-command", action="store_true", help="Print the official SWE-bench evaluation command after writing predictions.")
parser.add_argument("--run-id", default="firstcoder-swe-lite", help="Run id for the official SWE-bench harness command.")
parser.add_argument("--max-workers", type=int, default=1, help="Worker count for the official SWE-bench harness command.")
```

Modify `main()` after writing predictions:

```python
    if args.print_harness_command:
        print(" ".join(build_harness_command(predictions_path=args.out, run_id=args.run_id, max_workers=args.max_workers)))
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_eval_swebench.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 6 for the SWE-bench Lite plan. Focus on whether the official harness command matches SWE-bench Lite docs and whether the CLI keeps generation separate from Docker evaluation.
```

If review passes:

```bash
git add firstcoder/eval/swebench.py tests/test_eval_swebench.py
git commit -m "feat(eval): add swebench harness command helper"
```

---

### Task 7: Smoke Fixture and README

**Files:**
- Create: `docs/SWE_LITE_RUNBOOK.md`
- Create: `tests/fixtures/swebench_lite_sample.jsonl`
- Test: existing eval tests

- [ ] **Step 1: Add a tiny local instance fixture**

Create `tests/fixtures/swebench_lite_sample.jsonl`:

```jsonl
{"instance_id":"local__sample-1","repo":"local/sample","base_commit":"HEAD","problem_statement":"Change VALUE from 1 to 2 in module.py."}
```

- [ ] **Step 2: Add runbook**

Create `docs/SWE_LITE_RUNBOOK.md`:

```markdown
# SWE-bench Lite Runbook

This project evaluates FirstCoder on SWE-bench Lite in two phases:

1. Generate `predictions.jsonl` with FirstCoder.
2. Feed that file to the official SWE-bench Docker harness.

## Generate Predictions

Prepare local task repositories under a shared root. Each repo directory name must match the SWE-bench `instance_id`.

```bash
python -m firstcoder.eval.swebench \
  --instances data/swebench_lite_instances.jsonl \
  --repos-root /tmp/firstcoder-swe-lite/repos \
  --out runs/firstcoder_swe_lite_predictions.jsonl \
  --provider openai \
  --model-name firstcoder \
  --max-instances 1 \
  --print-harness-command
```

The output JSONL uses the official SWE-bench fields:

```json
{"instance_id":"...","model_name_or_path":"firstcoder","model_patch":"diff --git ..."}
```

## Evaluate Predictions

Install the official harness in an environment with Docker available, then run:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path runs/firstcoder_swe_lite_predictions.jsonl \
  --max_workers 1 \
  --run_id firstcoder-swe-lite
```

Start with `--max-instances 1` and `--max_workers 1` because SWE-bench evaluation can be slow and disk-heavy.
```

- [ ] **Step 3: Run eval tests and CLI help**

Run:

```bash
pytest tests/test_eval_tasks.py tests/test_eval_patch.py tests/test_eval_adapter.py tests/test_eval_swebench.py -q
python -m firstcoder.eval.swebench --help
```

Expected: tests PASS and CLI help exits 0.

- [ ] **Step 4: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 7 for the SWE-bench Lite plan. Focus on whether the runbook is executable, honest about prerequisites, and aligned with the implemented CLI.
```

If review passes:

```bash
git add docs/SWE_LITE_RUNBOOK.md tests/fixtures/swebench_lite_sample.jsonl
git commit -m "docs(eval): add swebench lite runbook"
```

---

### Task 8: End-to-End Local Smoke Test

**Files:**
- Create: `tests/test_eval_swebench_smoke.py`
- Modify only if required by failing smoke test.

- [ ] **Step 1: Write a smoke test using a fake adapter**

Create `tests/test_eval_swebench_smoke.py`:

```python
import json
import subprocess
from pathlib import Path

from firstcoder.eval.swebench import load_instances_jsonl, run_instances, write_predictions_jsonl
from firstcoder.eval.tasks import CodingTask, CodingTaskResult


class FakeAdapter:
    def run_task(self, task: CodingTask) -> CodingTaskResult:
        (task.repo_path / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--binary"],
            cwd=task.repo_path,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        return CodingTaskResult(
            instance_id=task.instance_id,
            model_name_or_path="firstcoder-fake",
            model_patch=diff,
        )


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def test_local_prediction_generation_smoke(tmp_path: Path):
    repos_root = tmp_path / "repos"
    repo = repos_root / "local__sample-1"
    repo.mkdir(parents=True)
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test User"], repo)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run(["git", "add", "module.py"], repo)
    run(["git", "commit", "-m", "init"], repo)

    instances_path = tmp_path / "instances.jsonl"
    instances_path.write_text(
        json.dumps(
            {
                "instance_id": "local__sample-1",
                "repo": "local/sample",
                "base_commit": "HEAD",
                "problem_statement": "Change VALUE from 1 to 2 in module.py.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    results = run_instances(
        instances=load_instances_jsonl(instances_path),
        repos_root=repos_root,
        adapter=FakeAdapter(),
        max_instances=1,
    )
    out = tmp_path / "predictions.jsonl"
    write_predictions_jsonl(out, results)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["instance_id"] == "local__sample-1"
    assert payload["model_name_or_path"] == "firstcoder-fake"
    assert "diff --git a/module.py b/module.py" in payload["model_patch"]
```

- [ ] **Step 2: Run smoke test**

Run:

```bash
pytest tests/test_eval_swebench_smoke.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 4: Review and commit**

Dispatch a review subagent with this prompt:

```text
Review Task 8 for the SWE-bench Lite plan. Focus on end-to-end confidence: local instance loading, repo cleanliness, adapter run, patch output, and official prediction JSONL shape.
```

If review passes:

```bash
git add tests/test_eval_swebench_smoke.py
git commit -m "test(eval): add swebench prediction smoke test"
```

---

## Manual First Run

After all tasks pass, run one real or near-real instance.

```bash
python -m firstcoder.eval.swebench \
  --instances data/swebench_lite_instances.jsonl \
  --repos-root /tmp/firstcoder-swe-lite/repos \
  --out runs/firstcoder_swe_lite_predictions.jsonl \
  --provider openai \
  --model-name firstcoder \
  --max-instances 1 \
  --session-root /tmp/firstcoder-swe-lite/sessions \
  --print-harness-command
```

Then evaluate the generated prediction with the official harness:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path runs/firstcoder_swe_lite_predictions.jsonl \
  --max_workers 1 \
  --run_id firstcoder-swe-lite-smoke
```

## Final Verification

Run:

```bash
pytest -q
python -m firstcoder.eval.swebench --help
python -m firstcoder.eval.swebench \
  --instances tests/fixtures/swebench_lite_sample.jsonl \
  --repos-root /tmp/firstcoder-swe-lite-smoke/repos \
  --out /tmp/firstcoder-swe-lite-smoke/predictions.jsonl \
  --max-instances 1 \
  --print-harness-command
```

The last command requires a prepared local repo at `/tmp/firstcoder-swe-lite-smoke/repos/local__sample-1`. If that repo is not prepared, it should fail clearly before invoking the model.

## Risks and Follow-ups

- SWE-bench official evaluation is Docker-heavy; prediction generation should remain separate from Docker evaluation.
- Real SWE-bench task checkout/reset is intentionally out of MVP scope. Add it next with `datasets.load_dataset("princeton-nlp/SWE-bench_Lite", split="test")` and deterministic clone/reset logic.
- Some issues require test-specific environment setup. The first adapter should let the agent inspect repo files and run local tests, but official scoring remains the source of truth.
- Long-running agents need timeout and budget controls. Add `--timeout-seconds` and `--max-tool-rounds` after the first working smoke run.
- If generated patches are empty, write the empty prediction line only when you want the official harness to skip/filter it. Otherwise report it as a local generation failure.
