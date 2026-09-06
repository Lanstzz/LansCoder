"""Small, dependency-free models for local Observatory traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence


class TraceStatus(StrEnum):
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ObservationType(StrEnum):
    AGENT = "agent"
    GENERATION = "generation"
    TOOL = "tool"
    EVENT = "event"


class ObservationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    SCHEDULED = "scheduled"


@dataclass(frozen=True, slots=True)
class TraceScope:
    """The immutable parent context captured at an execution boundary."""

    session_id: str
    branch_id: str
    parent_trace_id: str | None = None
    parent_observation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        if not self.branch_id:
            raise ValueError("branch_id must be a non-empty string")

    def activate(self, *, trace_id: str | None = None, observation_id: str | None = None):
        from .context import trace_context

        return trace_context(self, trace_id=trace_id, observation_id=observation_id)


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    trace_id: str
    session_id: str
    branch_id: str
    observation_type: ObservationType
    parent_observation_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None
    error: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_id: str
    session_id: str
    branch_id: str | None = None
    parent_trace_id: str | None = None
    parent_observation_id: str | None = None
    status: TraceStatus = TraceStatus.RUNNING
    incomplete: bool = False
    started_at: str | None = None
    ended_at: str | None = None
    outcome: str | None = None
    final_output: Any = None
    output_ref: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    reason: dict[str, Any] | None = None
    observations: tuple[Observation, ...] = ()

    @classmethod
    def from_events(
        cls,
        events: Sequence[Any],
        trace_id: str,
        *,
        payload_store: Any | None = None,
    ) -> "TraceRecord":
        """Project one trace without making the journal a mutable state store."""

        selected = [event for event in events if _event_value(event, "trace_id") == trace_id]
        started = next((event for event in selected if _event_value(event, "kind") == "trace.started"), None)
        if started is None:
            raise ValueError(f"trace {trace_id!r} has no trace.started event")

        started_data = _event_data(started)
        branch_id = _event_value(started, "branch_id")
        parent_trace_id = started_data.get("parent_trace_id")
        parent_observation_id = _event_value(started, "parent_observation_id") or started_data.get("parent_observation_id")
        status = TraceStatus.RUNNING
        incomplete = False
        ended = None
        outcome = None
        final_output = None
        output_ref = None
        error = None
        reason = None
        observations: dict[str, Observation] = {}
        paused = False

        for event in sorted(selected, key=lambda item: _event_value(item, "sequence") or 0):
            kind = _event_value(event, "kind")
            data = _event_data(event)
            if kind == "trace.started":
                branch_id = _event_value(event, "branch_id") or branch_id
                status = TraceStatus.RUNNING
                paused = False
            elif kind == "trace.paused":
                status = TraceStatus.WAITING_FOR_INPUT
                paused = True
            elif kind == "trace.resumed":
                status = TraceStatus.RUNNING
                paused = False
            elif kind == "trace.ended":
                ended = event
                outcome = data.get("outcome")
                status = _status_for_end(data)
                paused = False
                final_output = data.get("final_output")
                output_ref = _mapping_or_none(data.get("output_ref"))
                error = _mapping_or_none(data.get("error"))
                reason = _mapping_or_none(data.get("reason"))
            elif kind == "observability.failed" and (
                _event_value(event, "trace_id") == trace_id or data.get("trace_id") == trace_id
            ):
                incomplete = True
            elif kind == "observation.started":
                observation = _observation_from_started(event)
                observations[observation.observation_id] = observation
            elif kind == "observation.ended":
                observation_id = _event_value(event, "observation_id")
                if observation_id in observations:
                    observations[observation_id] = _observation_from_ended(observations[observation_id], event)

        if ended is None and not paused:
            incomplete = True
        if output_ref is not None and payload_store is not None:
            try:
                try:
                    payload_store.read(output_ref)
                except (TypeError, KeyError):
                    payload_store.read(output_ref["sha256"])
            except Exception:  # noqa: BLE001 - a missing payload is an evidence gap, not a projection failure.
                incomplete = True
        if ended is not None and outcome == "succeeded" and final_output is None and output_ref is None:
            incomplete = True

        return cls(
            trace_id=trace_id,
            session_id=str(_event_value(started, "session_id") or ""),
            branch_id=branch_id,
            parent_trace_id=parent_trace_id,
            parent_observation_id=parent_observation_id,
            status=status,
            incomplete=incomplete,
            started_at=_event_value(started, "occurred_at"),
            ended_at=_event_value(ended, "occurred_at") if ended is not None else None,
            outcome=outcome,
            final_output=final_output,
            output_ref=output_ref,
            error=error,
            reason=reason,
            observations=tuple(observations.values()),
        )


def project_trace(events: Sequence[Any], trace_id: str, *, payload_store: Any | None = None) -> TraceRecord:
    return TraceRecord.from_events(events, trace_id, payload_store=payload_store)


def _status_for_end(data: Mapping[str, Any]) -> TraceStatus:
    status = data.get("status")
    if status in {item.value for item in TraceStatus}:
        return TraceStatus(status)
    if data.get("outcome") == "cancelled":
        return TraceStatus.CANCELLED
    if data.get("outcome") == "failed":
        return TraceStatus.FAILED
    return TraceStatus.COMPLETED


def _observation_from_started(event: Any) -> Observation:
    data = _event_data(event)
    observation_type = data.get("observation_type", data.get("type", ObservationType.EVENT))
    try:
        observation_type = ObservationType(observation_type)
    except ValueError:
        observation_type = ObservationType.EVENT
    return Observation(
        observation_id=str(_event_value(event, "observation_id") or ""),
        trace_id=str(_event_value(event, "trace_id") or ""),
        session_id=str(_event_value(event, "session_id") or ""),
        branch_id=str(_event_value(event, "branch_id") or ""),
        observation_type=observation_type,
        parent_observation_id=_event_value(event, "parent_observation_id") or data.get("parent_observation_id"),
        started_at=_event_value(event, "occurred_at"),
        data={key: value for key, value in data.items() if key != "observation_type"},
    )


def _observation_from_ended(observation: Observation, event: Any) -> Observation:
    data = _event_data(event)
    return Observation(
        observation_id=observation.observation_id,
        trace_id=observation.trace_id,
        session_id=observation.session_id,
        branch_id=observation.branch_id,
        observation_type=observation.observation_type,
        parent_observation_id=observation.parent_observation_id,
        started_at=observation.started_at,
        ended_at=_event_value(event, "occurred_at"),
        duration_ms=data.get("duration_ms"),
        outcome=data.get("outcome"),
        error=_mapping_or_none(data.get("error")),
        data={**observation.data, **{key: value for key, value in data.items() if key not in {"duration_ms", "outcome", "error"}}},
    )


def _event_value(event: Any | None, name: str) -> Any:
    if event is None:
        return None
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _event_data(event: Any) -> dict[str, Any]:
    value = _event_value(event, "data")
    if value is None:
        value = _event_value(event, "payload")
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


Trace = TraceRecord


__all__ = [
    "Observation",
    "ObservationOutcome",
    "ObservationType",
    "TraceRecord",
    "Trace",
    "TraceScope",
    "TraceStatus",
    "project_trace",
]
