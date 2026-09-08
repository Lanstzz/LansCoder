"""Context-local trace state and restart-safe resume lookup."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from inspect import signature
from typing import Any, Protocol

from .models import TraceScope


active_trace_scope: ContextVar[TraceScope | None] = ContextVar("active_trace_scope", default=None)
active_trace_id: ContextVar[str | None] = ContextVar("active_trace_id", default=None)
active_observation_id: ContextVar[str | None] = ContextVar("active_observation_id", default=None)


def get_trace_scope() -> TraceScope | None:
    return active_trace_scope.get()


def get_trace_id() -> str | None:
    return active_trace_id.get()


def get_observation_id() -> str | None:
    return active_observation_id.get()


@contextmanager
def trace_context(scope: TraceScope, *, trace_id: str | None = None, observation_id: str | None = None):
    scope_token = active_trace_scope.set(scope)
    trace_token = active_trace_id.set(trace_id)
    observation_token = active_observation_id.set(observation_id)
    try:
        yield scope
    finally:
        active_observation_id.reset(observation_token)
        active_trace_id.reset(trace_token)
        active_trace_scope.reset(scope_token)


class TraceResumeLookup(Protocol):
    def find_resumable_trace(
        self,
        session_id: str,
        branch_id: str,
        *,
        tool_call_id: str | None = None,
        pending_kind: str | None = None,
    ) -> str | None: ...


class JournalTraceResumeLookup:
    """Find a paused trace from the append-only journal after a restart."""

    def __init__(self, journal: Any) -> None:
        self.journal = journal

    def find_resumable_trace(
        self,
        session_id: str,
        branch_id: str,
        *,
        tool_call_id: str | None = None,
        pending_kind: str | None = None,
    ) -> str | None:
        events = self._events(session_id)
        pending_by_trace: dict[str, dict[str, Any]] = {}
        active: set[str] = set()
        for event in events:
            if _event_value(event, "branch_id") != branch_id:
                continue
            kind = _event_value(event, "kind")
            trace_id = _event_value(event, "trace_id")
            if not isinstance(trace_id, str):
                continue
            data = _event_data(event)
            if kind == "trace.paused":
                pending_by_trace[trace_id] = data.get("pending") if isinstance(data.get("pending"), dict) else data
                active.add(trace_id)
            elif kind in {"trace.resumed", "trace.ended"}:
                active.discard(trace_id)
                pending_by_trace.pop(trace_id, None)
        candidates = [trace_id for trace_id in active if _pending_matches(pending_by_trace[trace_id], tool_call_id, pending_kind)]
        return candidates[0] if len(set(candidates)) == 1 else None

    def _events(self, session_id: str) -> list[Any]:
        method = getattr(self.journal, "read_events", None) or getattr(self.journal, "list_events", None)
        if method is None:
            return []
        return list(method(session_id) or [])


def _event_value(event: Any, name: str) -> Any:
    return event.get(name) if isinstance(event, dict) else getattr(event, name, None)


def _event_data(event: Any) -> dict[str, Any]:
    value = _event_value(event, "data")
    return dict(value) if isinstance(value, dict) else {}


def _pending_matches(pending: dict[str, Any], tool_call_id: str | None, pending_kind: str | None) -> bool:
    return (tool_call_id is None or pending.get("tool_call_id") == tool_call_id) and (pending_kind is None or pending.get("pending_kind") == pending_kind)


def lookup_resume_trace(
    lookup: Any,
    scope: TraceScope,
    *,
    tool_call_id: str | None = None,
    pending_kind: str | None = None,
) -> str | None:
    """Find a paused trace from durable state, never from ContextVar state alone."""

    method = getattr(lookup, "find_resumable_trace", None) or getattr(lookup, "lookup_resume_trace", None)
    if method is None:
        return None
    try:
        call_signature = signature(method)
    except (TypeError, ValueError):
        call_signature = None
    if call_signature is not None:
        try:
            call_signature.bind(scope.session_id, scope.branch_id, tool_call_id=tool_call_id, pending_kind=pending_kind)
        except TypeError:
            # An unsupported legacy lookup cannot satisfy the identity-aware contract.
            return None
    return method(scope.session_id, scope.branch_id, tool_call_id=tool_call_id, pending_kind=pending_kind)


def set_trace_context(scope: TraceScope, *, trace_id: str | None = None, observation_id: str | None = None) -> tuple[Token[Any], Token[Any], Token[Any]]:
    """Set state for adapters that cannot use a context manager."""

    return active_trace_scope.set(scope), active_trace_id.set(trace_id), active_observation_id.set(observation_id)


def reset_trace_context(tokens: tuple[Token[Any], Token[Any], Token[Any]]) -> None:
    scope_token, trace_token, observation_token = tokens
    active_observation_id.reset(observation_token)
    active_trace_id.reset(trace_token)
    active_trace_scope.reset(scope_token)


__all__ = [
    "TraceResumeLookup",
    "JournalTraceResumeLookup",
    "active_observation_id",
    "active_trace_id",
    "active_trace_scope",
    "get_observation_id",
    "get_trace_id",
    "get_trace_scope",
    "lookup_resume_trace",
    "reset_trace_context",
    "set_trace_context",
    "trace_context",
]
