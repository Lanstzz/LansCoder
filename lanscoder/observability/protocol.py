"""Low-level recorder contracts safe for agent/core code to depend on."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from lanscoder.journal import new_observation_id, new_trace_id

from .models import ObservationType, TraceScope


@runtime_checkable
class TraceIndex(Protocol):
    """Optional derived-index boundary; the journal remains the source of truth."""

    def update_event(self, event: Any) -> None: ...


@runtime_checkable
class TraceRecorder(Protocol):
    def start_trace(self, scope: TraceScope | None = None, *, trace_id: str | None = None, data: Mapping[str, Any] | None = None) -> str: ...

    def pause_trace(self, trace_id: str, *, pending: Mapping[str, Any] | None = None) -> None: ...

    def resume_trace(self, trace_id: str, *, scope: TraceScope | None = None) -> None: ...

    def end_trace(
        self,
        trace_id: str,
        *,
        status: str | None = None,
        outcome: str | None = None,
        final_output: Any = None,
        output_ref: Mapping[str, Any] | None = None,
        error: Any = None,
        reason: Any = None,
        no_generation: bool = False,
    ) -> None: ...

    def start_observation(
        self,
        trace_id: str,
        observation_type: ObservationType | str,
        *,
        observation_id: str | None = None,
        parent_observation_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        scope: TraceScope | None = None,
    ) -> str: ...

    def end_observation(self, observation_id: str, *, outcome: str, data: Mapping[str, Any] | None = None, error: Any = None, payload: Any = None) -> None: ...

    def link_trace(self, parent_trace_id: str, child_trace_id: str, *, relation: str = "child", data: Mapping[str, Any] | None = None, scope: TraceScope | None = None) -> None: ...

    def record_payload(
        self, value: Any, *, media_type: str = "application/json", force_reference: bool = False, trace_id: str | None = None, scope: TraceScope | None = None
    ) -> dict[str, Any] | None: ...


class NoOpTraceRecorder:
    """Recorder used when observability is disabled or not configured."""

    def start_trace(self, scope: TraceScope | None = None, *, trace_id: str | None = None, data: Mapping[str, Any] | None = None) -> str:
        return trace_id or new_trace_id()

    def pause_trace(self, trace_id: str, *, pending: Mapping[str, Any] | None = None) -> None:
        return None

    def resume_trace(self, trace_id: str, *, scope: TraceScope | None = None) -> None:
        return None

    def end_trace(
        self,
        trace_id: str,
        *,
        status: str | None = None,
        outcome: str | None = None,
        final_output: Any = None,
        output_ref: Mapping[str, Any] | None = None,
        error: Any = None,
        reason: Any = None,
        no_generation: bool = False,
    ) -> None:
        return None

    def start_observation(
        self,
        trace_id: str,
        observation_type: ObservationType | str,
        *,
        observation_id: str | None = None,
        parent_observation_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        scope: TraceScope | None = None,
    ) -> str:
        return observation_id or new_observation_id()

    def end_observation(self, observation_id: str, *, outcome: str, data: Mapping[str, Any] | None = None, error: Any = None, payload: Any = None) -> None:
        return None

    def record_payload(self, value: Any, *, media_type: str = "application/json", force_reference: bool = False, trace_id: str | None = None, scope: TraceScope | None = None) -> dict[str, Any] | None:
        return None

    def link_trace(self, parent_trace_id: str, child_trace_id: str, *, relation: str = "child", data: Mapping[str, Any] | None = None, scope: TraceScope | None = None) -> None:
        return None


NullTraceRecorder = NoOpTraceRecorder
NoOpRecorder = NoOpTraceRecorder

__all__ = ["NoOpRecorder", "NoOpTraceRecorder", "NullTraceRecorder", "TraceIndex", "TraceRecorder"]
