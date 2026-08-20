"""Stable protocol ports for agent orchestration boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lanscoder.context.manager import ContextCompactRequest, ContextCompactResult
from lanscoder.agent.user_input import AgentTurnResult


class ContextManagerLike(Protocol):
    """Minimal context-window manager surface used by AgentLoop."""

    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult: ...


@runtime_checkable
class SessionTurnRunner(Protocol):
    """The runner surface a child subagent loop must expose.

    ``SubagentEngine`` depends on this instead of importing ``AgentLoop``, which
    breaks the ``agent.loop -> agent.subagent -> agent.loop`` import cycle:
    the assembly root injects a factory that builds the concrete loop.
    """

    async def run_user_turn(self, content: str) -> AgentTurnResult: ...

    def usage_summary(self) -> dict[str, int]: ...
