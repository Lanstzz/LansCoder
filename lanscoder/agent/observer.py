from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from lanscoder.runtime.cancellation import CancellationToken

if TYPE_CHECKING:
    from lanscoder.agent.tool_execution import ToolExecutionEvent
    from lanscoder.providers.types import ChatStreamEvent


class ToolEventSink(Protocol):

    def on_tool_event(self, event: ToolExecutionEvent) -> None: ...


class TurnObserver:

    def __init__(
        self,
        *,
        stream_event_handler: Callable[[ChatStreamEvent], None] | None = None,
        tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        foreground_progress_provider: Callable[[], dict[str, Any] | None] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self._stream_event_handler = stream_event_handler
        self._tool_event_handler = tool_event_handler
        self._progress_callback = progress_callback
        self._foreground_progress_provider = foreground_progress_provider
        self.cancellation_token = cancellation_token
        self._provider_calls = 0
        self._total_tokens = 0

    def on_turn_started(self) -> None:
        pass

    def on_progress(self, provider_calls: int, total_tokens: int) -> None:
        self._provider_calls = provider_calls
        self._total_tokens = total_tokens
        if self._progress_callback is not None:
            self._progress_callback({"provider_calls": provider_calls, "total_tokens": total_tokens})

    def on_tool_event(self, event: ToolExecutionEvent) -> None:
        if self._tool_event_handler is None:
            return
        self._tool_event_handler(event)

    def on_stream_event(self, event: ChatStreamEvent) -> None:
        if self._stream_event_handler is None:
            return
        self._stream_event_handler(event)

    def foreground_progress(self) -> dict[str, Any] | None:
        if self._foreground_progress_provider is None:
            return None
        return self._foreground_progress_provider()

    def usage_summary(self) -> dict[str, int]:
        return {
            "provider_calls": self._provider_calls,
            "total_tokens": self._total_tokens,
        }

    def set_stream_event_handler(self, handler: Callable[[ChatStreamEvent], None] | None) -> None:
        self._stream_event_handler = handler

    def set_tool_event_handler(self, handler: Callable[[ToolExecutionEvent], None] | None) -> None:
        self._tool_event_handler = handler

    def replace_cancellation_token(self, token: CancellationToken | None) -> None:
        self.cancellation_token = token
