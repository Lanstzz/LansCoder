from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class PermissionAction(StrEnum):

    READ_PATH = "read_path"
    WRITE_PATH = "write_path"
    DELETE_PATH = "delete_path"
    EXECUTE_SHELL = "execute_shell"
    NETWORK_REQUEST = "network_request"
    GIT_OPERATION = "git_operation"
    READ_ENV = "read_env"
    MCP_TOOL = "mcp_tool"


class PermissionMode(StrEnum):

    STANDARD = "standard"
    AGGRESSIVE = "aggressive"
    BYPASS = "bypass"


class PermissionDecisionKind(StrEnum):

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionPersistence(StrEnum):

    ONCE = "once"
    ALWAYS = "always"


class PermissionScopeType(StrEnum):

    EXACT_PATH = "exact_path"
    PATH_TREE = "path_tree"
    COMMAND_PREFIX = "command_prefix"
    HOST = "host"
    ENV_KEY = "env_key"
    MCP_TOOL = "mcp_tool"


class PermissionConfirmationChoice(StrEnum):

    DENY = "deny"
    REJECT_WITH_FEEDBACK = "reject_with_feedback"
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS_SAME_SCOPE = "allow_always_same_scope"


@dataclass(slots=True)
class PermissionRequest:

    id: str
    action: PermissionAction
    target: str
    reason: str = ""
    cwd: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PermissionGrant:

    id: str
    effect: Literal["allow", "deny"]
    action: PermissionAction
    scope_type: PermissionScopeType
    scope_value: str
    created_at: str
    reason: str = ""


@dataclass(slots=True)
class PermissionDecision:

    kind: PermissionDecisionKind
    persistence: PermissionPersistence = PermissionPersistence.ONCE
    reason: str = ""
    feedback: str = ""
    grant: PermissionGrant | None = None
