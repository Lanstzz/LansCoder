"""Command-line entry point for single-turn FirstCoder runs."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.app.factory import create_firstcoder_app


@dataclass(frozen=True, slots=True)
class CliConfig:
    project_root: Path
    data_root: Path | None
    session_id: str | None
    provider_name: str | None
    message: str
    max_tool_rounds: int | None = None


CliRunner = Callable[[CliConfig], str]


class ChatRunnerLike(Protocol):
    last_pending_input: object | None

    def run_user_turn(self, content: str):
        ...

    def resume_with_user_input(self, request_id: str, answer: str):
        ...


def read_message(message: str | None, *, stdin_text: str | None = None) -> str:
    """Return a user message from an argument or stdin."""

    if message is not None:
        return message.strip()
    text = sys.stdin.read() if stdin_text is None else stdin_text
    return text.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a single FirstCoder user turn.")
    parser.add_argument("--project", default=".", help="Project root for tools and AGENTS.md.")
    parser.add_argument("--data-root", default=None, help="Directory for FirstCoder session data.")
    parser.add_argument("--session-id", default=None, help="Session id to create or reuse.")
    parser.add_argument("--provider", default=None, help="Provider name override.")
    parser.add_argument("--message", default=None, help="Single user message. Reads stdin when omitted.")
    parser.add_argument("--interactive", action="store_true", help="Run a line-oriented interactive session.")
    parser.add_argument("--auto-approve", action="store_true", help="Automatically answer permission confirmations with allow_once.")
    parser.add_argument("--max-tool-rounds", type=_positive_int, default=None, help="Override per-turn tool round limit.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runner: CliRunner | None = None,
    stdin_text: str | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.interactive:
        config = CliConfig(
            project_root=Path(args.project),
            data_root=Path(args.data_root) if args.data_root is not None else None,
            session_id=args.session_id,
            provider_name=args.provider,
            message="",
            max_tool_rounds=args.max_tool_rounds,
        )
        try:
            app = create_cli_app(config)
            lines = stdin_text.splitlines() if stdin_text is not None else None
            run_repl(app.chat_runner, lines, auto_approve=args.auto_approve)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    message = read_message(args.message, stdin_text=stdin_text)
    if not message:
        print("error: message is required via --message or stdin", file=sys.stderr)
        return 2

    config = CliConfig(
        project_root=Path(args.project),
        data_root=Path(args.data_root) if args.data_root is not None else None,
        session_id=args.session_id,
        provider_name=args.provider,
        message=message,
        max_tool_rounds=args.max_tool_rounds,
    )
    run = runner or run_single_turn
    try:
        output = run(config)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


def run_single_turn(config: CliConfig) -> str:
    app = create_cli_app(config)
    response = app.chat_runner.run_user_turn(config.message)
    return response.content


def create_cli_app(config: CliConfig):
    provider = None
    if config.provider_name is not None:
        from firstcoder.providers.factory import create_provider

        provider = create_provider(config.provider_name)
    app = create_firstcoder_app(
        project_root=config.project_root,
        data_root=config.data_root,
        provider=provider,
        session_id=config.session_id,
    )
    if config.max_tool_rounds is not None:
        app.chat_runner.limits = AgentLoopLimits.default().with_max_tool_rounds(config.max_tool_rounds)
    return app


def run_repl(
    chat_runner: ChatRunnerLike,
    lines: Iterable[str] | None = None,
    *,
    auto_approve: bool = False,
) -> None:
    source = iter(lines) if lines is not None else _stdin_lines()
    pending = None
    for raw_line in source:
        line = raw_line.strip()
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break

        if pending is not None:
            response = chat_runner.resume_with_user_input(_pending_id(pending), line)
        else:
            response = chat_runner.run_user_turn(line)

        print(f"FirstCoder> {response.content}")
        pending = getattr(chat_runner, "last_pending_input", None)
        while pending is not None and auto_approve and _pending_kind(pending) == "permission_confirmation":
            print("Auto-approve> allow_once")
            response = chat_runner.resume_with_user_input(_pending_id(pending), "allow_once")
            print(f"FirstCoder> {response.content}")
            pending = getattr(chat_runner, "last_pending_input", None)

        if pending is not None:
            print(f"Permission> {_pending_question(pending)}")


def _stdin_lines():
    while True:
        try:
            yield input("You> ")
        except EOFError:
            break


def _pending_id(pending: object) -> str:
    return str(getattr(pending, "id"))


def _pending_question(pending: object) -> str:
    return str(getattr(pending, "question", "需要用户输入。"))


def _pending_kind(pending: object) -> str:
    return str(getattr(pending, "kind", ""))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed
