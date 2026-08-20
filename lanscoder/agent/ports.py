from __future__ import annotations

from typing import Protocol, runtime_checkable

from lanscoder.context.manager import ContextCompactRequest, ContextCompactResult
from lanscoder.agent.user_input import AgentTurnResult


class ContextManagerLike(Protocol):

    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult: ...


@runtime_checkable
class SessionTurnRunner(Protocol):

    async def run_user_turn(self, content: str) -> AgentTurnResult: ...

    def usage_summary(self) -> dict[str, int]: ...
