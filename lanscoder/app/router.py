from __future__ import annotations

from dataclasses import dataclass

from lanscoder.app.commands import CommandResult
from lanscoder.app.ports import CommandHandlerLike


@dataclass(slots=True)
class CompositeCommandHandler:
    handlers: list[CommandHandlerLike]

    def handle(self, text: str) -> CommandResult:
        handled_any = False
        for handler in self.handlers:
            result = handler.handle(text)
            if result.handled:
                return result
            handled_any = handled_any or result.handled
        if text.strip().startswith("/"):
            return CommandResult(handled=True, output=f"Unknown command: {' '.join(text.strip().split())}")
        return CommandResult(handled=handled_any)

    def all_commands(self) -> list[tuple[str, str]]:
        commands: list[tuple[str, str]] = []
        for handler in self.handlers:
            commands.extend(handler.commands())
        commands.sort(key=lambda x: x[0].removeprefix("/").lstrip())
        return commands
