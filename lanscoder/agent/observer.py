from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from lanscoder.utils.cancellation import CancellationToken
from lanscoder.observability.models import ObservationType, TraceScope
from lanscoder.observability.protocol import TraceRecorder

if TYPE_CHECKING:
    from lanscoder.agent.tool_execution import ToolExecutionEvent
    from lanscoder.providers.types import ChatStreamEvent, ToolCall


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
        trace_recorder: TraceRecorder | None = None,
        trace_id: str | None = None,
        trace_scope: TraceScope | None = None,
    ) -> None:
        self._stream_event_handler = stream_event_handler
        self._tool_event_handler = tool_event_handler
        self._progress_callback = progress_callback
        self._foreground_progress_provider = foreground_progress_provider
        self.cancellation_token = cancellation_token
        self._trace_recorder = trace_recorder
        self._trace_id = trace_id
        self._trace_scope = trace_scope
        self._tool_observations: dict[str, list[str]] = {}
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
        self._record_tool_event(event)
        if self._tool_event_handler is None:
            return
        self._tool_event_handler(event)

    def on_stream_event(self, event: ChatStreamEvent) -> None:
        if self._stream_event_handler is None:
            return
        self._stream_event_handler(event)

    def record_permission_decision(
        self,
        *,
        tool_call: ToolCall,
        permission_request_id: str,
        decision: str,
    ) -> None:
        """Record the user's permission choice as a child event when possible."""
        parent_observation_id = self._parent_tool_observation(tool_call.id)
        self._record_event_data(
            {
                "event": "permission_decision",
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "permission_request_id": permission_request_id,
                "permission_decision": decision,
            },
            outcome="succeeded",
            parent_observation_id=parent_observation_id,
        )

    def record_user_input(self, *, tool_call: ToolCall, request_id: str, event: str) -> None:
        """Record an ask_user answer without persisting the answer text."""
        self._record_event_data(
            {
                "event": event,
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "request_id": request_id,
                "answer_recorded": True,
            },
            outcome="succeeded",
            parent_observation_id=self._parent_tool_observation(tool_call.id),
        )

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

    def set_trace_context(
        self,
        recorder: TraceRecorder | None,
        trace_id: str | None,
        scope: TraceScope | None,
    ) -> None:
        """Replace the trace context when a suspended turn is resumed."""

        self._trace_recorder = recorder
        self._trace_id = trace_id
        self._trace_scope = scope

    def _record_tool_event(self, event: ToolExecutionEvent) -> None:
        recorder = self._trace_recorder
        trace_id = self._trace_id
        if recorder is None or trace_id is None:
            return

        tool_call_id = str(event.tool_call.id)
        if event.kind in {"started", "background_started"}:
            if not self._tool_observations.get(tool_call_id):
                observation_id = self._safe_start_observation(ObservationType.TOOL, _tool_start_data(event))
                if observation_id is None:
                    return
                self._tool_observations.setdefault(tool_call_id, []).append(observation_id)
            if event.kind == "background_started":
                self._record_event_observation(event, outcome="scheduled", parent_observation_id=self._tool_observations[tool_call_id][0])
                self._end_tool_observation(tool_call_id, outcome="scheduled", event=event)
            return

        if event.kind in {"prewrite_review", "permission_requested"}:
            observation_ids = self._tool_observations.get(tool_call_id)
            if not observation_ids:
                observation_id = self._safe_start_observation(ObservationType.TOOL, _tool_start_data(event))
                if observation_id is None:
                    return
                observation_ids = self._tool_observations.setdefault(tool_call_id, [])
                observation_ids.append(observation_id)
            outcome = "skipped" if event.kind == "permission_requested" else _prewrite_outcome(event)
            self._record_event_observation(event, outcome=outcome, parent_observation_id=observation_ids[0])
            if event.kind == "permission_requested":
                # Close the paused execution so a legal waiting trace remains
                # complete; a resumed execution starts a continuation record.
                self._end_tool_observation(tool_call_id, outcome="skipped", event=event)
            return

        if event.kind in {"denied", "interrupted", "finished"}:
            outcome = "succeeded" if event.kind == "finished" and event.result is not None and event.result.ok else "failed"
            if event.kind == "interrupted":
                outcome = "cancelled"
            if event.kind == "denied":
                outcome = "failed"
            observation_ids = self._tool_observations.get(tool_call_id)
            if not observation_ids:
                observation_id = self._safe_start_observation(ObservationType.TOOL, _tool_start_data(event))
                if observation_id is not None:
                    observation_ids = self._tool_observations.setdefault(tool_call_id, [])
                    observation_ids.append(observation_id)
            if observation_ids:
                self._record_event_observation(event, outcome=outcome, parent_observation_id=observation_ids[0])
                self._end_tool_observation(tool_call_id, outcome=outcome, event=event)
            else:
                self._record_event_observation(event, outcome=outcome)

    def _record_event_observation(
        self,
        event: ToolExecutionEvent,
        *,
        outcome: str,
        parent_observation_id: str | None = None,
    ) -> None:
        self._record_event_data(
            _tool_start_data(event),
            outcome=outcome,
            parent_observation_id=parent_observation_id,
            event=event,
        )

    def _record_event_data(
        self,
        data: dict[str, Any],
        *,
        outcome: str,
        parent_observation_id: str | None = None,
        event: ToolExecutionEvent | None = None,
    ) -> None:
        recorder = self._trace_recorder
        trace_id = self._trace_id
        if recorder is None or trace_id is None:
            return
        observation_id = self._safe_start_observation(
            ObservationType.EVENT,
            data,
            parent_observation_id=parent_observation_id,
        )
        if observation_id is None:
            return
        self._safe_end_observation(observation_id, outcome=outcome, event=event)

    def _parent_tool_observation(self, tool_call_id: str) -> str | None:
        observation_ids = self._tool_observations.get(tool_call_id)
        return observation_ids[0] if observation_ids else None

    def _end_tool_observation(self, tool_call_id: str, *, outcome: str, event: ToolExecutionEvent | None = None) -> None:
        recorder = self._trace_recorder
        if recorder is None:
            return
        observation_ids = self._tool_observations.get(tool_call_id)
        if not observation_ids:
            return
        observation_id = observation_ids.pop(0)
        self._safe_end_observation(observation_id, outcome=outcome, event=event)
        if not observation_ids:
            self._tool_observations.pop(tool_call_id, None)

    def _safe_start_observation(
        self,
        observation_type: ObservationType,
        data: dict[str, Any],
        *,
        parent_observation_id: str | None = None,
    ) -> str | None:
        recorder = self._trace_recorder
        trace_id = self._trace_id
        if recorder is None or trace_id is None:
            return None
        try:
            return recorder.start_observation(
                trace_id,
                observation_type,
                data=data,
                parent_observation_id=parent_observation_id,
                scope=self._trace_scope,
            )
        except Exception:
            return None

    def _safe_end_observation(self, observation_id: str, *, outcome: str, event: ToolExecutionEvent | None) -> None:
        recorder = self._trace_recorder
        if recorder is None:
            return
        try:
            recorder.end_observation(
                observation_id,
                outcome=outcome,
                data=_tool_end_data(event) if event is not None else None,
                error=_tool_error(event) if event is not None else None,
            )
        except Exception:
            return


def _tool_start_data(event: ToolExecutionEvent) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_call_id": event.tool_call.id,
        "tool_name": event.tool_call.name,
        "event": event.kind,
        "arguments": event.original_arguments if event.original_arguments is not None else event.tool_call.arguments,
    }
    if event.permission_request is not None:
        data["permission_request_id"] = event.permission_request.id
    if event.prewrite_review is not None:
        data["prewrite_review"] = event.prewrite_review
    return data


def _prewrite_outcome(event: ToolExecutionEvent) -> str:
    review = event.prewrite_review
    if isinstance(review, dict) and review.get("error"):
        return "failed"
    return "succeeded"


def _tool_end_data(event: ToolExecutionEvent | None) -> dict[str, Any] | None:
    if event is None or event.result is None:
        return None
    data = {
        "tool_call_id": event.tool_call.id,
        "tool_name": event.tool_call.name,
        "ok": event.result.ok,
        "result_type": type(event.result).__name__,
    }
    result_data = event.result.data or {}
    for key in (
        "request_id",
        "permission_request_id",
        "permission_decision",
        "request_type",
        "prewrite_review",
    ):
        if key in result_data:
            data[key] = result_data[key]
    if event.permission_request is not None:
        data["permission_request_id"] = event.permission_request.id
    if event.prewrite_review is not None:
        data["prewrite_review"] = event.prewrite_review
    return data


def _tool_error(event: ToolExecutionEvent | None) -> dict[str, str] | None:
    if event is None or event.result is None or event.result.ok:
        return None
    return {"code": "tool_failed", "message": event.result.error or event.result.content}
