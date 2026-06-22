"""Verification command detection for agent-loop guardrails."""

from __future__ import annotations

import shlex

from firstcoder.tools.types import ToolResult


_PACKAGE_TEST_COMMANDS = {
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("go", "test"),
    ("cargo", "test"),
}


def is_verification_command(command: str) -> bool:
    """Return True when a shell command looks like a project verification command."""

    stripped = command.strip()
    if not stripped:
        return False
    if _has_shell_control_operator(stripped):
        return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return False
    if not tokens:
        return False

    executable = _basename(tokens[0])
    if executable == "pytest":
        return True
    if executable.startswith("python") and len(tokens) >= 3 and tokens[1:3] == ["-m", "pytest"]:
        return True
    if len(tokens) >= 2 and (_basename(tokens[0]), tokens[1]) in _PACKAGE_TEST_COMMANDS:
        return True
    return False


def is_successful_verification_result(tool_name: str, result: ToolResult) -> bool:
    """Return True when a tool result proves that a verification command passed."""

    if tool_name not in {"shell", "diagnostics"}:
        return False
    if not result.ok:
        return False
    if result.data.get("exit_code") != 0:
        return False
    command = result.data.get("command")
    if not isinstance(command, str):
        return False
    return is_verification_command(command)


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _has_shell_control_operator(command: str) -> bool:
    return any(operator in command for operator in ("&&", "||", ";", "|", "\n", "&"))
