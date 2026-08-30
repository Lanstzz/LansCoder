from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class AgentLoopStopReason(StrEnum):
    TOOL_ROUND_LIMIT = "tool_round_limit"
    PROVIDER_CALL_LIMIT = "provider_call_limit"
    TURN_TIMEOUT = "turn_timeout"


@dataclass(frozen=True, slots=True)
class AgentLoopLimits:

    max_tool_rounds: int | None = 200
    max_provider_calls: int | None = 400
    max_turn_seconds: float | None = 3600

    @classmethod
    def default(cls) -> "AgentLoopLimits":
        return cls()

    @classmethod
    def benchmark(cls) -> "AgentLoopLimits":
        return cls(
            max_tool_rounds=120,
            max_provider_calls=120,
            max_turn_seconds=3600,
        )

    def with_max_tool_rounds(self, value: int | None) -> "AgentLoopLimits":
        return replace(self, max_tool_rounds=value)


class _AgentLoopLimitReached(Exception):

    def __init__(self, reason: AgentLoopStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
