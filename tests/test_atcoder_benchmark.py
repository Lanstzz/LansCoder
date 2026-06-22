import json
import subprocess
from pathlib import Path

import pytest

from benchmark.atcoder.runner import (
    AtCoderTask,
    OjSubmissionResult,
    build_acc_new_command,
    build_oj_submit_command,
    build_oj_test_command,
    build_parser,
    load_tasks_jsonl,
    main,
    task_directory,
    parse_oj_submission_output,
    write_summary_json,
)


def test_load_tasks_jsonl_reads_atcoder_fields(tmp_path: Path):
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps(
            {
                "contest": "abc086",
                "task": "abc086_a",
                "url": "https://atcoder.jp/contests/abc086/tasks/abc086_a",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_tasks_jsonl(path) == [
        AtCoderTask(
            contest="abc086",
            task="abc086_a",
            url="https://atcoder.jp/contests/abc086/tasks/abc086_a",
        )
    ]


def test_load_tasks_jsonl_derives_url_when_omitted(tmp_path: Path):
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps({"contest": "abc086", "task": "abc086_a"}) + "\n", encoding="utf-8")

    [task] = load_tasks_jsonl(path)

    assert task.url == "https://atcoder.jp/contests/abc086/tasks/abc086_a"


def test_build_cli_commands_use_expected_tools(tmp_path: Path):
    task = AtCoderTask(contest="abc086", task="abc086_a")

    assert build_acc_new_command(task, tmp_path) == ["acc", "new", "abc086", "--choice", "all"]
    assert build_oj_test_command(tmp_path / "abc086" / "a", "main.py") == [
        "oj",
        "test",
        "-c",
        "python3 main.py",
    ]
    assert build_oj_submit_command(task, tmp_path / "abc086" / "a", "main.py") == [
        "oj",
        "submit",
        "https://atcoder.jp/contests/abc086/tasks/abc086_a",
        "main.py",
        "--yes",
    ]


def test_task_directory_prefers_acc_short_task_dir(tmp_path: Path):
    task = AtCoderTask(contest="abc086", task="abc086_a")
    (tmp_path / "abc086" / "a").mkdir(parents=True)

    assert task_directory(tmp_path, task) == tmp_path / "abc086" / "a"


def test_task_directory_uses_full_task_dir_when_it_exists(tmp_path: Path):
    task = AtCoderTask(contest="practice2", task="practice2_a")
    (tmp_path / "practice2" / "practice2_a").mkdir(parents=True)

    assert task_directory(tmp_path, task) == tmp_path / "practice2" / "practice2_a"


def test_parser_requires_explicit_submit_to_hit_atcoder():
    parser = build_parser()

    args = parser.parse_args(["--tasks", "tasks.jsonl", "--workdir", "runs/atcoder"])

    assert args.submit is False


def test_parse_oj_submission_output_extracts_ac_verdict():
    output = """
    [SUCCESS] result:
    problem: https://atcoder.jp/contests/abc086/tasks/abc086_a
    ID 123456789
    Status AC
    Exec Time 17 ms
    Memory 9140 KB
    """

    assert parse_oj_submission_output(output) == OjSubmissionResult(
        verdict="AC",
        submission_id="123456789",
        raw_output=output,
    )


def test_parse_oj_submission_output_extracts_wa_verdict_from_url_line():
    output = "Submission URL: https://atcoder.jp/contests/abc086/submissions/123456789\nResult: WA\n"

    assert parse_oj_submission_output(output).verdict == "WA"
    assert parse_oj_submission_output(output).submission_id == "123456789"


def test_write_summary_json_serializes_results(tmp_path: Path):
    out = tmp_path / "summary.json"

    write_summary_json(
        out,
        [
            {
                "contest": "abc086",
                "task": "abc086_a",
                "samples_passed": True,
                "submitted": True,
                "verdict": "AC",
            }
        ],
    )

    assert json.loads(out.read_text(encoding="utf-8")) == [
        {
            "contest": "abc086",
            "task": "abc086_a",
            "samples_passed": True,
            "submitted": True,
            "verdict": "AC",
        }
    ]


def test_missing_cli_tool_error_mentions_install_hint(monkeypatch, tmp_path: Path):
    from benchmark.atcoder import runner

    def fail_run(*args, **kwargs):
        raise FileNotFoundError("oj")

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="pip install online-judge-tools"):
        runner.run_command(["oj", "test"], cwd=tmp_path)


def test_main_prints_runtime_error_without_traceback(monkeypatch, tmp_path: Path, capsys):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(json.dumps({"contest": "abc086", "task": "abc086_a"}) + "\n", encoding="utf-8")

    monkeypatch.setattr("benchmark.atcoder.runner.run_tasks", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    assert main(["--tasks", str(tasks), "--workdir", str(tmp_path / "work")]) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: boom\n"
