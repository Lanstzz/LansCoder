"""Context-local trace state and restart-safe resume lookup."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
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
    ) -> str | None:
        ...


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
        return method(
            scope.session_id,
            scope.branch_id,
            tool_call_id=tool_call_id,
            pending_kind=pending_kind,
        )
    except TypeError:
        return method(scope.session_id, scope.branch_id)


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
