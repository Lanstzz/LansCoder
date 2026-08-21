from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from lanscoder.providers.types import ToolDefinition


@dataclass(slots=True)
class ToolResult:

    name: str
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ToolExecutor(Protocol):

    def __call__(self, **kwargs: Any) -> ToolResult: ...


@dataclass(slots=True)
class Tool:

    definition: ToolDefinition
    executor: ToolExecutor

    @property
    def name(self) -> str:
        return self.definition.name


def make_error_result(name: str, message: str, **data: Any) -> ToolResult:

    return ToolResult(name=name, ok=False, content=message, data=data, error=message)


def make_text_result(name: str, content: str, **data: Any) -> ToolResult:

    return ToolResult(name=name, ok=True, content=content, data=data)
