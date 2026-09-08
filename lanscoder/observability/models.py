"""Small, dependency-free models for local Observatory traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from lanscoder.storage.payloads import PayloadRef

from .evidence import METADATA_FIELDS, PARAMETER_FIELDS, bounded_fields, bounded_tags


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
class TraceSummary:
    """Bounded fields suitable for trace list and query indexes."""

    trace_id: str
    session_id: str
    branch_id: str | None
    status: TraceStatus
    incomplete: bool
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    provider: str | None = None
    model: str | None = None
    tool_name: str | None = None
    total_tokens: int | None = None
    usage_details: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    has_error: bool = False
    observation_count: int = 0
    observation_counts: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "branch_id": self.branch_id,
            "status": self.status.value,
            "incomplete": self.incomplete,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "provider": self.provider,
            "model": self.model,
            "tool_name": self.tool_name,
            "total_tokens": self.total_tokens,
            "usage_details": dict(self.usage_details),
            "parameters": dict(self.parameters),
            "has_error": self.has_error,
            "observation_count": self.observation_count,
            "observation_counts": dict(self.observation_counts),
            "metadata": dict(self.metadata),
            "tags": list(self.tags),
        }


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
    duration_ms: int | None = None
    outcome: str | None = None
    final_output: Any = None
    output_ref: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    reason: dict[str, Any] | None = None
    observations: tuple[Observation, ...] = ()
    links: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None

    @classmethod
    def from_events(
        cls,
        events: Sequence[Any],
        trace_id: str,
        *,
        payload_store: Any | None = None,
    ) -> "TraceRecord":
        """Project one trace without making the journal a mutable state store."""

        selected = [
            event
            for event in events
            if _event_value(event, "trace_id") == trace_id
            or (_event_value(event, "kind") == "trace.linked" and trace_id in {_event_data(event).get("parent_trace_id"), _event_data(event).get("child_trace_id")})
        ]
        started = next((event for event in selected if _event_value(event, "kind") == "trace.started"), None)
        if started is None:
            raise ValueError(f"trace {trace_id!r} has no trace.started event")

        selected.extend(
            event
            for event in events
            if _event_value(event, "kind") == "journal.recovered" and _event_value(event, "session_id") == _event_value(started, "session_id") and _event_value(event, "trace_id") != trace_id
        )

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
        duration_ms = None
        observations: dict[str, Observation] = {}
        paused = False
        started_seen = False
        links: list[dict[str, Any]] = []

        for event in sorted(selected, key=lambda item: _event_value(item, "sequence") or 0):
            kind = _event_value(event, "kind")
            data = _event_data(event)
            if kind == "journal.recovered":
                if _event_value(event, "session_id") != _event_value(started, "session_id"):
                    continue
                targets = [_event_value(event, "trace_id"), data.get("trace_id"), data.get("affected_trace_id")]
                for key in ("trace_ids", "affected_trace_ids"):
                    if isinstance(data.get(key), (list, tuple)):
                        targets.extend(data[key])
                explicit_ids = {value for value in targets if isinstance(value, str) and value}
                if explicit_ids:
                    incomplete = incomplete or trace_id in explicit_ids
                elif started_seen and _event_value(event, "branch_id") in (None, branch_id):
                    # A tail without identity can only implicate execution open
                    # at that sequence, not completed or legally paused turns.
                    incomplete = incomplete or status == TraceStatus.RUNNING or any(observation.ended_at is None for observation in observations.values())
                continue
            if data.get("evidence_incomplete") or (kind != "trace.linked" and _event_value(event, "branch_id") is not None and _event_value(event, "branch_id") != branch_id):
                incomplete = True
            for reference in _payload_refs(data):
                try:
                    if payload_store is None:
                        raise ValueError("payload store is unavailable")
                    payload_store.read(PayloadRef.from_dict(reference))
                except Exception:
                    incomplete = True
            if kind == "trace.started":
                started_seen = True
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
                ended_status = _status_for_end(data)
                if ended_status is None:
                    incomplete = True
                    continue
                ended = event
                outcome = data.get("outcome")
                status = ended_status
                paused = False
                final_output = data.get("final_output")
                output_ref = _mapping_or_none(data.get("output_ref"))
                error = _mapping_or_none(data.get("error"))
                reason = _mapping_or_none(data.get("reason"))
                duration_ms = data.get("duration_ms")
            elif kind == "observability.failed" and (_event_value(event, "trace_id") == trace_id or data.get("trace_id") == trace_id):
                incomplete = True
            elif kind == "observation.started":
                observation = _observation_from_started(event)
                observations[observation.observation_id] = observation
            elif kind == "observation.ended":
                observation_id = _event_value(event, "observation_id")
                if observation_id in observations:
                    observations[observation_id] = _observation_from_ended(observations[observation_id], event)
            elif kind == "trace.linked":
                parent = data.get("parent_trace_id")
                child = data.get("child_trace_id")
                if parent == trace_id or child == trace_id:
                    links.append(dict(data))

        if ended is None and not paused:
            incomplete = True
        if ended is not None and status == TraceStatus.COMPLETED and outcome != "no_generation" and final_output is None and output_ref is None:
            incomplete = True
        if ended is not None and (status in {TraceStatus.FAILED, TraceStatus.CANCELLED} or outcome == "no_generation") and not error and not reason:
            incomplete = True
        if any(observation.ended_at is None for observation in observations.values()):
            incomplete = True

        metadata = bounded_fields(started_data.get("metadata"), METADATA_FIELDS)
        metadata.update(bounded_fields(started_data, METADATA_FIELDS))
        tags = bounded_tags(started_data.get("tags"))

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
            duration_ms=duration_ms,
            outcome=outcome,
            final_output=final_output,
            output_ref=output_ref,
            error=error,
            reason=reason,
            observations=tuple(observations.values()),
            metadata=metadata,
            tags=tags,
            provider=_string_value(started_data.get("provider")),
            model=_string_value(started_data.get("model")),
            links=tuple(links),
        )

    def to_summary(self) -> TraceSummary:
        provider = self.provider
        model = self.model
        tool_name = None
        total_tokens = None
        usage_details: dict[str, Any] = {}
        parameters: dict[str, Any] = {}
        observation_counts: dict[str, int] = {}
        for observation in self.observations:
            name = observation.observation_type.value
            observation_counts[name] = observation_counts.get(name, 0) + 1
            provider = provider or _string_value(observation.data.get("provider"))
            model = model or _string_value(observation.data.get("model"))
            tool_name = tool_name or _string_value(observation.data.get("tool_name"))
            usage = observation.data.get("usage")
            if isinstance(usage, Mapping) and observation.observation_type == ObservationType.GENERATION:
                if isinstance(usage.get("total_tokens"), int) and not isinstance(usage["total_tokens"], bool):
                    total_tokens = (total_tokens or 0) + usage["total_tokens"]
                details = usage.get("usage_details")
                if isinstance(details, Mapping):
                    usage_details = _add_numeric_fields(usage_details, details)
            parameters.update(bounded_fields(observation.data.get("parameters"), PARAMETER_FIELDS))
            request = observation.data.get("normalized_request")
            if isinstance(request, Mapping):
                for key in ("temperature", "max_tokens", "max_completion_tokens", "reasoning_effort", "tool_choice"):
                    if key in request and _is_flat_json_value(request[key]):
                        parameters[key] = request[key]
        return TraceSummary(
            trace_id=self.trace_id,
            session_id=self.session_id,
            branch_id=self.branch_id,
            status=self.status,
            incomplete=self.incomplete,
            started_at=self.started_at,
            ended_at=self.ended_at,
            duration_ms=self.duration_ms,
            provider=provider,
            model=model,
            tool_name=tool_name,
            total_tokens=total_tokens,
            usage_details=usage_details,
            parameters=parameters,
            has_error=self.error is not None or any(observation.error is not None for observation in self.observations),
            observation_count=len(self.observations),
            observation_counts=observation_counts,
            metadata=dict(self.metadata),
            tags=self.tags,
        )


def project_trace(events: Sequence[Any], trace_id: str, *, payload_store: Any | None = None) -> TraceRecord:
    return TraceRecord.from_events(events, trace_id, payload_store=payload_store)


def _status_for_end(data: Mapping[str, Any]) -> TraceStatus | None:
    status = data.get("status")
    if status in {TraceStatus.RUNNING.value, TraceStatus.WAITING_FOR_INPUT.value}:
        return None
    if status in {TraceStatus.COMPLETED.value, TraceStatus.FAILED.value, TraceStatus.CANCELLED.value}:
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


def _bounded_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            if not isinstance(item, str) or len(item) <= 256:
                result[key] = item
        if len(result) == 32:
            break
    return result


def _bounded_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item and len(item) <= 64 and item not in result:
            result.append(item)
        if len(result) == 32:
            break
    return tuple(result)


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_flat_json_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _add_numeric_fields(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping):
            result[key] = _add_numeric_fields(result.get(key, {}) if isinstance(result.get(key, {}), Mapping) else {}, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = result.get(key)
            result[key] = previous + value if isinstance(previous, (int, float)) and not isinstance(previous, bool) else value
    return result


def _payload_refs(value: Any):
    if isinstance(value, Mapping):
        if {"sha256", "media_type", "size_bytes"} <= value.keys():
            yield dict(value)
        else:
            for item in value.values():
                yield from _payload_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _payload_refs(item)


Trace = TraceRecord


__all__ = [
    "Observation",
    "ObservationOutcome",
    "ObservationType",
    "TraceRecord",
    "TraceSummary",
    "Trace",
    "TraceScope",
    "TraceStatus",
    "project_trace",
]
