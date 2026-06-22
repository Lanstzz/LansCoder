import json
from pathlib import Path

from benchmark.local_pytest.runner import (
    LocalPytestTask,
    build_parser,
    load_tasks_jsonl,
    materialize_task_repo,
    run_tasks,
    run_pytest,
    write_summary_json,
)
from firstcoder.eval.tasks import CodingTask, CodingTaskResult


def test_load_tasks_jsonl_reads_local_task(tmp_path: Path):
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "demo",
                "title": "Demo Task",
                "files": {"src/demo.py": "VALUE = 1\n"},
                "problem_statement": "Make VALUE equal 2.",
                "test_command": "python -m pytest -q",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_tasks_jsonl(path) == [
        LocalPytestTask(
            id="demo",
            title="Demo Task",
            files={"src/demo.py": "VALUE = 1\n"},
            problem_statement="Make VALUE equal 2.",
            test_command="python -m pytest -q",
        )
    ]


def test_materialize_task_repo_creates_git_repo_with_files(tmp_path: Path):
    task = LocalPytestTask(
        id="demo",
        title="Demo Task",
        files={"src/demo.py": "VALUE = 1\n", "tests/test_demo.py": "def test_demo(): pass\n"},
        problem_statement="Fix it.",
    )

    repo = materialize_task_repo(task, tmp_path)

    assert (repo / ".git").exists()
    assert "__pycache__/" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert (repo / "src" / "demo.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert run_pytest(repo, "python -m pytest -q").passed


def test_run_pytest_reports_failure(tmp_path: Path):
    task = LocalPytestTask(
        id="demo",
        title="Demo Task",
        files={"tests/test_demo.py": "def test_demo():\n    assert False\n"},
        problem_statement="Fix it.",
    )
    repo = materialize_task_repo(task, tmp_path)

    result = run_pytest(repo, "python -m pytest -q")

    assert result.passed is False
    assert result.returncode == 1
    assert "assert False" in result.output


def test_write_summary_json_serializes_rows(tmp_path: Path):
    out = tmp_path / "summary.json"

    write_summary_json(out, [{"id": "demo", "passed": True}])

    assert json.loads(out.read_text(encoding="utf-8")) == [{"id": "demo", "passed": True}]


def test_parser_defaults_to_sample_tasks():
    parser = build_parser()

    args = parser.parse_args(["--workdir", "runs/local-pytest"])

    assert args.tasks == "benchmark/local_pytest/tasks.sample.jsonl"
    assert args.max_tasks is None


def test_run_tasks_scores_agent_changes_and_writes_summary(tmp_path: Path):
    class FixingAdapter:
        def run_task(self, task: CodingTask) -> CodingTaskResult:
            (task.repo_path / "src" / "demo.py").write_text("VALUE = 2\n", encoding="utf-8")
            return CodingTaskResult(
                instance_id=task.instance_id,
                model_name_or_path="fake",
                model_patch="",
                raw_response="done",
            )

    task = LocalPytestTask(
        id="demo",
        title="Demo Task",
        files={
            "src/demo.py": "VALUE = 1\n",
            "tests/test_demo.py": "from src.demo import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        },
        problem_statement="Make VALUE equal 2.",
    )
    summary = tmp_path / "summary.json"

    rows = run_tasks(
        tasks=[task],
        workdir=tmp_path / "work",
        summary_out=summary,
        adapter=FixingAdapter(),
    )

    assert rows[0]["passed"] is True
    assert "+VALUE = 2" in rows[0]["model_patch"]
    assert json.loads(summary.read_text(encoding="utf-8"))[0]["passed"] is True
