from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lanscoder.app.commands import CommandResult

if TYPE_CHECKING:
    from lanscoder.app.router import CompositeCommandHandler


@dataclass(slots=True)
class HelpCommandHandler:

    command_handler: CompositeCommandHandler

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command != "/help":
            return CommandResult(handled=False)
        all_commands = self.command_handler.all_commands()
        lines = ["Commands:", *[f"{cmd.ljust(48)} {desc}" for cmd, desc in all_commands]]
        return CommandResult(handled=True, output="\n".join(lines))

    def commands(self) -> list[tuple[str, str]]:
        return [("/help", "Show available commands.")]
