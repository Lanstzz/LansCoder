from pathlib import Path

from dataclasses import dataclass, field

from firstcoder.cli import CliConfig, main, read_message, run_repl


@dataclass
class FakeResponse:
    content: str
    finish_reason: str = "stop"


@dataclass
class FakePending:
    id: str
    kind: str
    question: str


@dataclass
class FakeChatRunner:
    replies: list[FakeResponse]
    pending_after_turn: FakePending | None = None
    pending_after_resume: list[FakePending | None] | None = None
    turns: list[str] = field(default_factory=list)
    resumes: list[tuple[str, str]] = field(default_factory=list)
    last_pending_input: FakePending | None = None

    def run_user_turn(self, content: str) -> FakeResponse:
        self.turns.append(content)
        self.last_pending_input = self.pending_after_turn
        return self.replies.pop(0)

    def resume_with_user_input(self, request_id: str, answer: str) -> FakeResponse:
        self.resumes.append((request_id, answer))
        if self.pending_after_resume is None:
            self.last_pending_input = None
        else:
            self.last_pending_input = self.pending_after_resume.pop(0)
        return self.replies.pop(0)


def test_read_message_prefers_argument_over_stdin():
    assert read_message("hello", stdin_text="ignored") == "hello"


def test_read_message_reads_stdin_when_message_missing():
    assert read_message(None, stdin_text="hello from stdin\n") == "hello from stdin"


def test_main_runs_single_message_with_injected_runner(tmp_path: Path, capsys):
    seen: list[CliConfig] = []

    def fake_runner(config: CliConfig) -> str:
        seen.append(config)
        return "done"

    exit_code = main(
        [
            "--project",
            str(tmp_path),
            "--data-root",
            str(tmp_path / ".fc"),
            "--session-id",
            "cli_test",
            "--message",
            "solve it",
        ],
        runner=fake_runner,
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "done\n"
    assert seen == [
        CliConfig(
            project_root=tmp_path,
            data_root=tmp_path / ".fc",
            session_id="cli_test",
            provider_name=None,
            message="solve it",
            max_tool_rounds=None,
        )
    ]


def test_main_returns_error_for_empty_message(tmp_path: Path, capsys):
    exit_code = main(["--project", str(tmp_path)], stdin_text="")

    assert exit_code == 2
    assert "message is required" in capsys.readouterr().err


def test_run_repl_sends_multiple_user_messages(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("first reply"),
            FakeResponse("second reply"),
        ]
    )

    run_repl(runner, ["hello", "continue"])

    assert runner.turns == ["hello", "continue"]
    assert capsys.readouterr().out == "FirstCoder> first reply\nFirstCoder> second reply\n"


def test_run_repl_routes_next_line_to_pending_permission(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need permission", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow?"),
    )

    run_repl(runner, ["write file", "allow_once"])

    assert runner.turns == ["write file"]
    assert runner.resumes == [("perm_1", "allow_once")]
    assert capsys.readouterr().out == (
        "FirstCoder> need permission\n"
        "Permission> Allow?\n"
        "FirstCoder> done\n"
    )


def test_run_repl_auto_approves_repeated_permissions(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need first", finish_reason="waiting_for_user_input"),
            FakeResponse("need second", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow first?"),
        pending_after_resume=[FakePending(id="perm_2", kind="permission_confirmation", question="Allow second?"), None],
    )

    run_repl(runner, ["work"], auto_approve=True)

    assert runner.turns == ["work"]
    assert runner.resumes == [("perm_1", "allow_once"), ("perm_2", "allow_once")]
    assert "Auto-approve> allow_once" in capsys.readouterr().out


def test_main_parses_max_tool_rounds_for_single_message(tmp_path: Path):
    seen: list[CliConfig] = []

    def fake_runner(config: CliConfig) -> str:
        seen.append(config)
        return "done"

    exit_code = main(
        [
            "--project",
            str(tmp_path),
            "--message",
            "solve it",
            "--max-tool-rounds",
            "80",
        ],
        runner=fake_runner,
    )

    assert exit_code == 0
    assert seen[0].max_tool_rounds == 80
