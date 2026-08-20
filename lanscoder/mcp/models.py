from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping


class McpConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class McpLocalServerConfig:

    name: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_ms: int = 5000
    allowed_tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, slots=True)
class McpRemoteServerConfig:

    name: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    bearer_token_env_var: str | None = None
    enabled: bool = True
    timeout_ms: int = 5000
    allowed_tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class McpServerStatus:

    name: str
    state: Literal["disabled", "connecting", "connected", "failed"]
    tool_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class McpToolDescription:

    name: str
    description: str | None
    input_schema: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
