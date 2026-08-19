from lanscoder.app.commands import CommandResult
from lanscoder.app.help_commands import HelpCommandHandler
from lanscoder.app.router import CompositeCommandHandler


class _StubHandler:
    """A stub handler that returns predefined commands for testing."""

    def __init__(self, commands: list[tuple[str, str]]) -> None:
        self._commands = commands

    def handle(self, text: str) -> CommandResult:
        return CommandResult(handled=False)

    def commands(self) -> list[tuple[str, str]]:
        return self._commands


def test_help_command_lists_current_slash_commands() -> None:
    stub_handlers = [
        _StubHandler(
            [
                ("/new [title]", "Start a new session."),
                ("/fork [title]", "Copy the current session into a new branch."),
                ("/sessions", "List saved sessions."),
                ("/session <session_id>", "Show one session summary."),
                ("/resume", "Pick a session to resume."),
                ("/resume <session_id>", "Resume a session directly."),
                ("/share [session_id] [--tool-results]", "Export a shareable transcript."),
                ("/rename <title>", "Rename the current session."),
                ("/model", "Pick a model to use."),
                ("/model <model|provider/model>", "Switch the active model."),
                ("/skills", "Pick a skill to reference."),
                ("/skill <name>", "Show skill details."),
                ("/context", "Inspect context state."),
                ("/compact status", "Show compaction status."),
                ("/compact", "Compact context now."),
                ("/mode", "Show permission mode."),
                ("/mode <standard|aggressive|bypass>", "Change permission mode."),
            ]
        ),
    ]
    cmd_handler = CompositeCommandHandler(stub_handlers)
    result = HelpCommandHandler(command_handler=cmd_handler).handle("/help")

    assert result.handled is True
    assert "Commands:" in result.output
    expected = {
        "/new [title]": "Start a new session.",
        "/fork [title]": "Copy the current session into a new branch.",
        "/sessions": "List saved sessions.",
        "/session <session_id>": "Show one session summary.",
        "/resume": "Pick a session to resume.",
        "/resume <session_id>": "Resume a session directly.",
        "/share [session_id] [--tool-results]": "Export a shareable transcript.",
        "/rename <title>": "Rename the current session.",
        "/model": "Pick a model to use.",
        "/model <model|provider/model>": "Switch the active model.",
        "/skills": "Pick a skill to reference.",
        "/skill <name>": "Show skill details.",
        "/context": "Inspect context state.",
        "/compact status": "Show compaction status.",
        "/compact": "Compact context now.",
        "/mode": "Show permission mode.",
        "/mode <standard|aggressive|bypass>": "Change permission mode.",
    }
    for command, description in expected.items():
        assert f"{command.ljust(48)} {description}" in result.output


def test_help_command_ignores_non_help_input() -> None:
    cmd_handler = CompositeCommandHandler([])
    result = HelpCommandHandler(command_handler=cmd_handler).handle("/sessions")

    assert result.handled is False


def test_all_commands_sorted_alphabetically() -> None:
    handler_a = _StubHandler([("/zebra", "Z command.")])
    handler_b = _StubHandler([("/alpha", "A command.")])
    cmd_handler = CompositeCommandHandler([handler_a, handler_b])
    all_commands = cmd_handler.all_commands()
    names = [cmd for cmd, _desc in all_commands]
    assert names == sorted(names, key=lambda x: x.removeprefix("/").lstrip())
