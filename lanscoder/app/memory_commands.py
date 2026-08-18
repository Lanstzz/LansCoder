"""/memory slash command：列出、新增、删除持久记忆。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lanscoder.app.commands import CommandResult
from lanscoder.context.writer import SessionEventWriter
from lanscoder.memory.index import MemoryIndex
from lanscoder.memory.manager import MemoryManager
from lanscoder.memory.models import MemoryRecord, MemoryScope


@dataclass(slots=True)
class MemoryCommandHandler:
    """处理 `/memory` 系列命令。"""

    memory_provider: Callable[[], MemoryManager | None]
    writer_provider: Callable[[], SessionEventWriter | None] | None = None

    def commands(self) -> list[tuple[str, str]]:
        return [
            ("/memory", "List memories."),
            ("/memory remember <name>: <body>", "Add a memory."),
            ("/memory forget [user:]<name>", "Delete a memory."),
        ]

    def handle(self, text: str) -> CommandResult:
        command = text.strip()
        if not command.startswith("/memory"):
            return CommandResult(handled=False)
        manager = self.memory_provider()
        if manager is None:
            return CommandResult(handled=True, output="Memory unavailable: no memory manager configured")
        normalized = " ".join(command.split())
        if normalized == "/memory":
            return CommandResult(handled=True, output=_render_all(manager))
        if normalized.startswith("/memory remember "):
            return CommandResult(handled=True, output=_remember(manager, self.writer_provider, normalized))
        if normalized.startswith("/memory forget "):
            return CommandResult(handled=True, output=_forget(manager, self.writer_provider, normalized))
        return CommandResult(
            handled=True,
            output="Usage: /memory | /memory remember <name>: <body> | /memory forget [user:]<name>",
        )


def _render_all(manager: MemoryManager) -> str:
    user = manager.list(MemoryScope.USER)
    project = manager.list(MemoryScope.PROJECT)
    lines = ["User memory:"]
    lines.append(MemoryIndex.render(user) if user else "  (empty)")
    lines.append("")
    lines.append("Project memory:")
    lines.append(MemoryIndex.render(project) if project else "  (empty)")
    return "\n".join(lines)


def _remember(manager: MemoryManager, writer_provider, normalized: str) -> str:
    rest = normalized[len("/memory remember ") :]
    name, sep, body = rest.partition(":")
    name = name.strip()
    body = body.strip()
    if not sep or not name or not body:
        return "Usage: /memory remember <name>: <body>"
    try:
        manager.write(
            MemoryScope.PROJECT,
            MemoryRecord(name=name, description=_first_line(body), type="project", body=body),
        )
    except ValueError as exc:
        return f"Unable to remember: {exc}"
    _append_event(writer_provider, "project", name, "upsert")
    return f"Saved project memory '{name}'."


def _forget(manager: MemoryManager, writer_provider, normalized: str) -> str:
    arg = normalized[len("/memory forget ") :].strip()
    scope = MemoryScope.USER if arg.startswith("user:") else MemoryScope.PROJECT
    name = arg[len("user:") :].strip() if scope is MemoryScope.USER else arg
    if not name:
        return "Usage: /memory forget [user:]<name>"
    if not manager.delete(scope, name):
        return f"No {scope.value} memory named '{name}' found."
    _append_event(writer_provider, scope.value, name, "delete")
    return f"Forgot {scope.value} memory '{name}'."


def _first_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()[:80]
    return body[:80]


def _append_event(writer_provider, scope: str, name: str, action: str) -> None:
    if writer_provider is None:
        return
    writer = writer_provider()
    if writer is not None:
        writer.append_event("memory_updated", {"scope": scope, "name": name, "action": action})
