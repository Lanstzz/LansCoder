"""Fail-open journal recorder for trace and observation lifecycle facts."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Callable

from lanscoder.journal import JournalStore, new_observation_id, new_trace_id

from .context import get_observation_id, get_trace_scope
from .models import ObservationType, TraceScope


class JournalTraceRecorder:
    """Record observability facts without making storage part of agent control flow."""

    def __init__(
        self,
        journal: JournalStore,
        payload_store: Any | None = None,
        trace_index: Any | None = None,
        *,
        clock: Callable[[], str] | None = None,
        monotonic: Callable[[], float] | None = None,
        inline_payload_limit: int = 4096,
    ) -> None:
        self.journal = journal
        self.payload_store = payload_store
        self.trace_index = trace_index
        self._clock = clock or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._inline_payload_limit = inline_payload_limit
        self._started: dict[str, float] = {}
        self._observation_started: dict[str, float] = {}
        self._trace_scopes: dict[str, TraceScope] = {}
        self._observation_scopes: dict[str, tuple[str, TraceScope]] = {}
        self._failure_reported = False
        self.diagnostics: list[dict[str, str]] = []

    def start_trace(self, scope: TraceScope | None = None, *, trace_id: str | None = None, data: Mapping[str, Any] | None = None) -> str:
        resolved_scope = scope or get_trace_scope()
        identifier = trace_id or new_trace_id()
        if resolved_scope is None:
            self._failure("trace.started", "a TraceScope is required", trace_id=identifier)
            return identifier
        try:
            self._trace_scopes[identifier] = resolved_scope
            self._started[identifier] = self._monotonic()
            event_data = {"status": "running", **_json_safe_mapping(data)}
            if resolved_scope.parent_trace_id is not None:
                event_data["parent_trace_id"] = resolved_scope.parent_trace_id
            if resolved_scope.parent_observation_id is not None:
                event_data["parent_observation_id"] = resolved_scope.parent_observation_id
            self._write(
                "trace.started",
                event_data,
                scope=resolved_scope,
                trace_id=identifier,
                parent_observation_id=resolved_scope.parent_observation_id,
            )
        except Exception as error:  # noqa: BLE001 - recorder failure is deliberately fail-open.
            self._failure("trace.started", error, trace_id=identifier, scope=resolved_scope)
        return identifier

    def pause_trace(self, trace_id: str, *, pending: Mapping[str, Any] | None = None) -> None:
        scope = self._trace_scopes.get(trace_id) or get_trace_scope()
        self._attempt(
            "trace.paused",
            {"status": "waiting_for_input", "pending": _json_safe_mapping(pending)},
            scope=scope,
            trace_id=trace_id,
        )

    def resume_trace(self, trace_id: str) -> None:
        scope = self._trace_scopes.get(trace_id) or get_trace_scope()
        self._attempt("trace.resumed", {"status": "running"}, scope=scope, trace_id=trace_id)

    def end_trace(
        self,
        trace_id: str,
        *,
        status: str | None = None,
        outcome: str | None = None,
        final_output: Any = None,
        error: Any = None,
        reason: Any = None,
        no_generation: bool = False,
    ) -> None:
        scope = self._trace_scopes.get(trace_id) or get_trace_scope()
        resolved_outcome = outcome or ("no_generation" if no_generation else _outcome_for_status(status))
        if resolved_outcome == "succeeded" and final_output is None:
            resolved_outcome = "no_generation"
            reason = reason or {"code": "no_generation", "message": "trace ended without generated output"}
        resolved_status = status or _status_for_outcome(resolved_outcome)
        data: dict[str, Any] = {"status": resolved_status, "outcome": resolved_outcome}
        if final_output is not None:
            encoded = self._encode_output(final_output, trace_id=trace_id, scope=scope)
            if encoded is not None:
                data.update(encoded)
        if error is not None:
            data["error"] = _structured_error(error)
        if reason is not None:
            data["reason"] = _structured_error(reason)
        started_at = self._started.get(trace_id)
        if started_at is not None:
            data["duration_ms"] = max(0, int((self._monotonic() - started_at) * 1000))
        self._attempt("trace.ended", data, scope=scope, trace_id=trace_id)

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
        identifier = observation_id or new_observation_id()
        resolved_scope = scope or self._trace_scopes.get(trace_id) or get_trace_scope()
        parent = parent_observation_id or (resolved_scope.parent_observation_id if resolved_scope else None) or get_observation_id()
        try:
            if resolved_scope is None:
                raise ValueError("a TraceScope is required for an observation")
            self._observation_started[identifier] = self._monotonic()
            self._observation_scopes[identifier] = (trace_id, resolved_scope)
            event_data = {"observation_type": str(observation_type), **_json_safe_mapping(data)}
            if parent is not None:
                event_data["parent_observation_id"] = parent
            self._write(
                "observation.started",
                event_data,
                scope=resolved_scope,
                trace_id=trace_id,
                observation_id=identifier,
                parent_observation_id=parent,
            )
        except Exception as error:  # noqa: BLE001 - see class contract.
            self._failure("observation.started", error, trace_id=trace_id, scope=resolved_scope)
        return identifier

    def end_observation(
        self,
        observation_id: str,
        *,
        outcome: str,
        data: Mapping[str, Any] | None = None,
        error: Any = None,
        payload: Any | None = None,
    ) -> None:
        trace_id, scope = self._observation_scopes.get(observation_id, (None, None))
        event_data = {"outcome": outcome, **_json_safe_mapping(data)}
        if error is not None:
            event_data["error"] = _structured_error(error)
        if payload is not None:
            reference = self.record_payload(payload, trace_id=trace_id, scope=scope)
            if reference is not None:
                event_data["payload_ref"] = reference
        started_at = self._observation_started.get(observation_id)
        if started_at is not None:
            event_data["duration_ms"] = max(0, int((self._monotonic() - started_at) * 1000))
        self._attempt(
            "observation.ended",
            event_data,
            scope=scope or get_trace_scope(),
            trace_id=trace_id,
            observation_id=observation_id,
        )

    def link_trace(
        self,
        parent_trace_id: str,
        child_trace_id: str,
        *,
        relation: str = "child",
        data: Mapping[str, Any] | None = None,
        scope: TraceScope | None = None,
    ) -> None:
        resolved_scope = scope or self._trace_scopes.get(parent_trace_id) or self._trace_scopes.get(child_trace_id) or get_trace_scope()
        event_data = {"parent_trace_id": parent_trace_id, "child_trace_id": child_trace_id, "relation": relation, **_json_safe_mapping(data)}
        self._attempt("trace.linked", event_data, scope=resolved_scope, trace_id=child_trace_id)

    def record_payload(
        self,
        value: Any,
        *,
        media_type: str = "application/json",
        force_reference: bool = False,
        trace_id: str | None = None,
        scope: TraceScope | None = None,
    ) -> dict[str, Any] | None:
        if self.payload_store is None:
            return None
        try:
            safe_value = _json_safe(value)
            encoded = json.dumps(safe_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if not force_reference and len(encoded.encode("utf-8")) <= self._inline_payload_limit:
                return None
            put_json = getattr(self.payload_store, "put_json", None)
            reference = put_json(safe_value, media_type=media_type) if put_json is not None else self.payload_store.put(encoded, media_type=media_type)
            return _payload_reference(reference)
        except Exception as error:  # noqa: BLE001 - payload storage is an optional evidence path.
            self._failure("payload", error, trace_id=trace_id, scope=scope)
            return None

    def _encode_output(self, value: Any, *, trace_id: str, scope: TraceScope | None) -> dict[str, Any] | None:
        safe_value = _json_safe(value)
        encoded = json.dumps(safe_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self.payload_store is not None and len(encoded.encode("utf-8")) > self._inline_payload_limit:
            reference = self.record_payload(safe_value, force_reference=True, trace_id=trace_id, scope=scope)
            return {"output_ref": reference} if reference is not None else {"final_output": safe_value}
        return {"final_output": safe_value}

    def _attempt(self, kind: str, data: Mapping[str, Any], *, scope: TraceScope | None, trace_id: str | None = None, observation_id: str | None = None) -> None:
        try:
            if scope is None:
                raise ValueError(f"a TraceScope is required for {kind}")
            self._write(kind, data, scope=scope, trace_id=trace_id, observation_id=observation_id)
        except Exception as error:  # noqa: BLE001 - recorder failure is fail-open by design.
            self._failure(kind, error, trace_id=trace_id, scope=scope)

    def _write(
        self,
        kind: str,
        data: Mapping[str, Any],
        *,
        scope: TraceScope,
        trace_id: str | None = None,
        observation_id: str | None = None,
        parent_observation_id: str | None = None,
    ) -> Any:
        event = self.journal.append(
            kind,
            _json_safe_mapping(data),
            session_id=scope.session_id,
            trace_id=trace_id,
            observation_id=observation_id,
            parent_observation_id=parent_observation_id,
            branch_id=scope.branch_id,
        )
        self._update_index(event)
        return event

    def _update_index(self, event: Any) -> None:
        if self.trace_index is None:
            return
        updater = getattr(self.trace_index, "update_event", None) or getattr(self.trace_index, "update", None) or getattr(self.trace_index, "upsert", None)
        if updater is not None:
            try:
                updater(event)
            except Exception as error:  # noqa: BLE001 - indexes are disposable projections.
                trace_id = getattr(event, "trace_id", None)
                self._failure("index", error, trace_id=trace_id, scope=self._trace_scopes.get(trace_id))

    def _failure(self, operation: str, error: Any, *, trace_id: str | None = None, scope: TraceScope | None = None) -> None:
        message = str(error)
        self.diagnostics.append({"operation": operation, "error": message})
        if self._failure_reported:
            return
        self._failure_reported = True
        try:
            self.journal.append(
                "observability.failed",
                {"operation": operation, "error": {"type": type(error).__name__, "message": message}},
                session_id=scope.session_id if scope is not None else self._session_id_from_journal(),
                trace_id=trace_id,
                branch_id=scope.branch_id if scope is not None else None,
            )
        except Exception:
            return

    def _session_id_from_journal(self) -> str:
        session_id = getattr(self.journal, "session_id", None)
        if session_id:
            return str(session_id)
        return "observability"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _outcome_for_status(status: str | None) -> str:
    return {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}.get(status or "", "succeeded")


def _status_for_outcome(outcome: str) -> str:
    return {"failed": "failed", "cancelled": "cancelled"}.get(outcome, "completed")


def _structured_error(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    return {"code": "reason", "message": str(value)}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict())
        except Exception:
            pass
    return {"type": type(value).__name__, "repr": repr(value)}


def _json_safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return _json_safe(value or {})


def _payload_reference(reference: Any) -> dict[str, Any]:
    if isinstance(reference, Mapping):
        return _json_safe_mapping(reference)
    to_dict = getattr(reference, "to_dict", None)
    if callable(to_dict):
        return _json_safe_mapping(to_dict())
    return {"reference": _json_safe(reference)}


__all__ = ["JournalTraceRecorder"]
