"""Turn-level budget guardrails: pure policy plus counter ownership.

``TurnGuardrails`` owns the per-turn provider-call counter and turn-start clock
and turns limit hits into ``ChatResponse`` values.  It holds no session or loop
state, so the loop can delegate limits to it without entangling policy with
orchestration.
"""

from __future__ import annotations

import time

from lanscoder.agent.loop_limits import (
    _AgentLoopLimitReached,
    AgentLoopLimits,
    AgentLoopStopReason,
)
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatResponse


class TurnGuardrails:
    """Owns the per-turn counters and turns budget hits into responses.

    ``begin_turn`` resets the call counter and records the turn start so a
    resumed turn keeps its original timeout budget.  ``reserve_call`` and
    ``check_timeout`` raise ``_AgentLoopLimitReached``; ``limit_response`` and
    ``interrupted_response`` build the graceful-exit ``ChatResponse`` values.
    """

    def __init__(
        self,
        *,
        provider: ChatProvider,
        limits: AgentLoopLimits,
        clock=time.monotonic,
    ) -> None:
        self._provider = provider
        self._limits = limits
        self._clock = clock
        self._provider_call_count = 0
        self._turn_started_at: float | None = None

    def begin_turn(self) -> None:
        """Reset the turn counters and record the turn start time."""
        self._provider_call_count = 0
        self._turn_started_at = self._clock()

    def reserve_call(self) -> None:
        """Reserve one provider call, raising when the call limit is reached."""
        limit = self._limits.max_provider_calls
        if limit is not None and self._provider_call_count >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.PROVIDER_CALL_LIMIT)
        self._provider_call_count += 1

    def check_timeout(self) -> None:
        """Raise when the current turn has run past ``max_turn_seconds``."""
        limit = self._limits.max_turn_seconds
        if limit is None or self._turn_started_at is None:
            return
        if self._clock() - self._turn_started_at >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.TURN_TIMEOUT)

    @property
    def call_count(self) -> int:
        """Read-only provider-call count for the current turn."""
        return self._provider_call_count

    def limit_response(self, reason: AgentLoopStopReason, *, raw: dict | None = None) -> ChatResponse:
        """Build the graceful-exit response for a reached budget limit."""
        messages = {
            AgentLoopStopReason.PROVIDER_CALL_LIMIT: (f"provider 调用次数达到上限（max_provider_calls={self._limits.max_provider_calls}），已停止继续执行。"),
            AgentLoopStopReason.TURN_TIMEOUT: (f"本轮任务耗时达到上限（max_turn_seconds={self._limits.max_turn_seconds}），已停止继续执行。"),
            AgentLoopStopReason.TOOL_ROUND_LIMIT: (f"工具调用轮次达到上限（max_tool_rounds={self._limits.max_tool_rounds}），已停止继续执行工具。"),
        }
        return ChatResponse(
            provider=self._provider.name,
            model=self._provider.model,
            content=messages[reason],
            tool_calls=[],
            finish_reason=reason.value,
            raw=raw,
        )

    def interrupted_response(self) -> ChatResponse:
        return ChatResponse(
            provider=self._provider.name,
            model=self._provider.model,
            content="当前任务已中断。",
            tool_calls=[],
            finish_reason="interrupted",
            raw={"interrupted": True},
        )
