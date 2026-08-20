from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from lanscoder.providers.types import ToolDefinition

if TYPE_CHECKING:
    from lanscoder.permissions.types import PermissionAction


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
class ToolPermissionSpec:

    action: PermissionAction
    target_arg: str | None = None
    target_value: str | None = None
    target_builder: Callable[[dict[str, Any]], str] | None = None
    cwd_arg: str | None = None
    reason: str = ""
    allow_always: bool = True
    allow_auto: bool = True


@dataclass(slots=True)
class Tool:

    definition: ToolDefinition
    executor: ToolExecutor
    permission: ToolPermissionSpec | None = None

    @property
    def name(self) -> str:
        return self.definition.name


def make_error_result(name: str, message: str, **data: Any) -> ToolResult:

    return ToolResult(name=name, ok=False, content=message, data=data, error=message)


def make_text_result(name: str, content: str, **data: Any) -> ToolResult:

    return ToolResult(name=name, ok=True, content=content, data=data)
