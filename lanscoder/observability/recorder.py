"""Fail-open journal recorder for trace and observation lifecycle facts."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Callable

from lanscoder.journal import JournalStore, new_observation_id, new_trace_id
from lanscoder.journal.models import utc_now_iso

from .context import get_observation_id, get_trace_id, get_trace_scope
from .evidence import METADATA_FIELDS, PARAMETER_FIELDS, STREAM_FIELDS, bounded_fields, bounded_tags, json_safe, serializable_raw
from .index import JournalTraceIndex
from .models import ObservationType, TraceScope


class JournalTraceRecorder:
    """Only optional observability writes enter this failure boundary."""

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
        if trace_index is None and isinstance(journal, JournalStore):
            self.trace_index = JournalTraceIndex(journal.paths, payload_store=payload_store)
        self._clock = clock or utc_now_iso
        self._monotonic = monotonic or time.monotonic
        self._inline_payload_limit = inline_payload_limit
        self._started: dict[str, float] = {}
        self._observation_started: dict[str, float] = {}
        self._trace_scopes: dict[str, TraceScope] = {}
        self._observation_scopes: dict[str, tuple[str, TraceScope]] = {}
        self._observation_types: dict[str, str] = {}
        self._streaming: set[str] = set()
        self._failed_traces: set[str] = set()
        self._failure_reported = False
        self.diagnostics: list[dict[str, str]] = []

    def start_trace(self, scope: TraceScope | None = None, *, trace_id: str | None = None, data: Mapping[str, Any] | None = None) -> str:
        identifier = trace_id or new_trace_id()
        scope = scope or get_trace_scope()
        try:
            if scope is None:
                raise ValueError("a TraceScope is required")
            self._trace_scopes[identifier] = scope
            self._started[identifier] = self._monotonic()
            event_data = {**json_safe(data or {}), "status": "running"}
            if scope.parent_trace_id is not None:
                event_data["parent_trace_id"] = scope.parent_trace_id
            if scope.parent_observation_id is not None:
                event_data["parent_observation_id"] = scope.parent_observation_id
            self._write("trace.started", event_data, scope=scope, trace_id=identifier, parent_observation_id=scope.parent_observation_id)
        except Exception as error:
            self._failure("trace.started", error, trace_id=identifier, scope=scope)
        return identifier

    def pause_trace(self, trace_id: str, *, pending: Mapping[str, Any] | None = None) -> None:
        scope = self._trace_scopes.get(trace_id) or get_trace_scope()
        try:
            self._write("trace.paused", {"status": "waiting_for_input", "pending": {**json_safe(pending or {}), "trace_id": trace_id}}, scope=scope, trace_id=trace_id)
        except Exception as error:
            self._failure("trace.paused", error, trace_id=trace_id, scope=scope)

    def resume_trace(self, trace_id: str, *, scope: TraceScope | None = None) -> None:
        scope = self._trace_scopes.get(trace_id) or scope or get_trace_scope()
        try:
            if scope is None:
                raise ValueError("a TraceScope is required")
            self._trace_scopes[trace_id] = scope
            self._write("trace.resumed", {"status": "running"}, scope=scope, trace_id=trace_id)
        except Exception as error:
            self._failure("trace.resumed", error, trace_id=trace_id, scope=scope)

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
    ) -> bool:
        scope = self._trace_scopes.get(trace_id) or get_trace_scope()
        try:
            resolved_outcome = outcome or ("no_generation" if no_generation else {"failed": "failed", "cancelled": "cancelled"}.get(status, "succeeded"))
            data: dict[str, Any] = {
                "status": status or {"failed": "failed", "cancelled": "cancelled"}.get(resolved_outcome, "completed"),
                "outcome": resolved_outcome,
            }
            if data["status"] not in {"completed", "failed", "cancelled"}:
                raise ValueError("end_trace requires a terminal status; use pause_trace for waiting")
            if final_output is not None:
                safe_output = json_safe(final_output)
                encoded = json.dumps(safe_output, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > self._inline_payload_limit:
                    reference = self.record_payload(safe_output, force_reference=True, trace_id=trace_id, scope=scope)
                    if reference is not None:
                        data["output_ref"] = reference
                if "output_ref" not in data:
                    data["final_output"] = safe_output
            elif output_ref is not None:
                data["output_ref"] = json_safe(output_ref)
            if error is not None:
                data["error"] = _structured_error(error)
            if reason is not None:
                data["reason"] = _structured_error(reason)
            if trace_id in self._started:
                data["duration_ms"] = max(0, int((self._monotonic() - self._started[trace_id]) * 1000))
            self._write("trace.ended", data, scope=scope, trace_id=trace_id)
            return True
        except Exception as failure:
            self._failure("trace.ended", failure, trace_id=trace_id, scope=scope)
            return False

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
        scope = self._trace_scopes.get(trace_id) or scope or get_trace_scope()
        try:
            if scope is None:
                raise ValueError("a TraceScope is required")
            self._observation_scopes[identifier] = (trace_id, scope)
            self._observation_types[identifier] = ObservationType(observation_type).value
            self._observation_started[identifier] = self._monotonic()
            parent = parent_observation_id
            if parent is None and get_trace_id() == trace_id:
                parent = get_observation_id()
            parent = parent or scope.parent_observation_id
            event_data = self._prepare_observation_data(identifier, data, trace_id, scope, starting=True)
            event_data["observation_type"] = str(observation_type)
            self._write("observation.started", event_data, scope=scope, trace_id=trace_id, observation_id=identifier, parent_observation_id=parent)
        except Exception as error:
            self._failure("observation.started", error, trace_id=trace_id, scope=scope)
        return identifier

    def end_observation(self, observation_id: str, *, outcome: str, data: Mapping[str, Any] | None = None, error: Any = None, payload: Any = None) -> None:
        trace_id, scope = self._observation_scopes.get(observation_id, (None, None))
        try:
            event_data = self._prepare_observation_data(observation_id, data, trace_id, scope, starting=False)
            event_data["outcome"] = outcome
            if error is not None:
                event_data["error"] = _structured_error(error)
            if payload is not None:
                small_text = isinstance(payload, str) and len(payload.encode("utf-8")) <= self._inline_payload_limit
                if small_text:
                    event_data["payload"] = payload
                else:
                    reference = self.record_payload(payload, force_reference=True, trace_id=trace_id, scope=scope)
                    if reference is not None:
                        event_data["payload_ref"] = reference
                    else:
                        event_data["evidence_incomplete"] = True
            if observation_id in self._observation_started:
                event_data["duration_ms"] = max(0, int((self._monotonic() - self._observation_started[observation_id]) * 1000))
            self._write("observation.ended", event_data, scope=scope, trace_id=trace_id, observation_id=observation_id)
        except Exception as failure:
            self._failure("observation.ended", failure, trace_id=trace_id, scope=scope)

    def link_trace(self, parent_trace_id: str, child_trace_id: str, *, relation: str = "child", data: Mapping[str, Any] | None = None, scope: TraceScope | None = None) -> None:
        child_scope = self._trace_scopes.get(child_trace_id) or scope or self._trace_scopes.get(parent_trace_id) or get_trace_scope()
        parent_scope = self._trace_scopes.get(parent_trace_id)
        try:
            event_data = {**json_safe(data or {}), "parent_trace_id": parent_trace_id, "child_trace_id": child_trace_id, "relation": relation}
            self._write("trace.linked", event_data, scope=child_scope, trace_id=child_trace_id)
            if parent_scope is not None and child_scope is not None and (parent_scope.session_id, parent_scope.branch_id) != (child_scope.session_id, child_scope.branch_id):
                self._write("trace.linked", event_data, scope=parent_scope, trace_id=parent_trace_id)
        except Exception as error:
            self._failure("trace.linked", error, trace_id=child_trace_id, scope=child_scope)

    def record_payload(self, value: Any, *, media_type: str = "application/json", force_reference: bool = False, trace_id: str | None = None, scope: TraceScope | None = None) -> dict[str, Any] | None:
        scope = self._trace_scopes.get(trace_id) or scope or get_trace_scope()
        try:
            if not force_reference and isinstance(value, str) and len(value.encode("utf-8")) <= self._inline_payload_limit:
                return None
            if self.payload_store is None:
                raise ValueError("payload store is unavailable")
            if isinstance(value, (bytes, bytearray, memoryview)):
                reference = self.payload_store.put(bytes(value), media_type=media_type)
            else:
                reference = self.payload_store.put_json(json_safe(value), media_type=media_type)
            return dict(reference) if isinstance(reference, Mapping) else reference.to_dict()
        except Exception as error:
            self._failure("payload", error, trace_id=trace_id, scope=scope)
            return None

    def _prepare_observation_data(self, observation_id: str, data: Mapping[str, Any] | None, trace_id: str | None, scope: TraceScope | None, *, starting: bool) -> dict[str, Any]:
        source = data or {}
        prepared = json_safe(source)
        generation = self._observation_types.get(observation_id) == "generation"
        if not generation:
            prepared.pop("stream_summary", None)
            return prepared
        if source.get("streaming") or "stream_summary" in source:
            self._streaming.add(observation_id)
        if "normalized_request" in prepared:
            request = prepared["normalized_request"]
            parameters = bounded_fields(request, PARAMETER_FIELDS)
            if isinstance(request, Mapping):
                parameters.update(bounded_fields(request.get("extra_body"), PARAMETER_FIELDS))
            prepared["parameters"] = parameters
        key = "normalized_request" if starting else "normalized_response"
        if key not in prepared:
            prepared[key] = {"unavailable": True}
            prepared["evidence_incomplete"] = True
        raw = source.get("provider_raw_response")
        prepared.pop("provider_raw_response", None)
        if raw is not None and observation_id not in self._streaming:
            safe_raw = serializable_raw(raw)
            if safe_raw is not None:
                prepared["provider_raw_response"] = safe_raw
            else:
                prepared["provider_raw_response_summary"] = {"type": type(raw).__name__, "summary": "not JSON serializable"}
        if observation_id in self._streaming:
            prepared["stream_summary"] = bounded_fields(source.get("stream_summary", {}), STREAM_FIELDS)
        for key in ("normalized_request", "normalized_response", "provider_raw_response"):
            if key not in prepared or prepared[key] == {"unavailable": True}:
                continue
            reference = self.record_payload(prepared[key], force_reference=True, trace_id=trace_id, scope=scope)
            if reference is not None:
                prepared[key] = {"payload_ref": reference}
            else:
                prepared["evidence_incomplete"] = True
        return prepared

    def _write(self, kind: str, data: Mapping[str, Any], *, scope: TraceScope | None, trace_id: str | None = None, observation_id: str | None = None, parent_observation_id: str | None = None) -> Any:
        if scope is None:
            raise ValueError(f"a TraceScope is required for {kind}")
        value = json_safe(data)
        if "metadata" in value:
            value["metadata"] = bounded_fields(value["metadata"], METADATA_FIELDS)
        if "tags" in value:
            value["tags"] = list(bounded_tags(value["tags"]))
        if trace_id in self._failed_traces:
            value["evidence_incomplete"] = True
        event = self.journal.append(
            kind,
            value,
            session_id=scope.session_id,
            trace_id=trace_id,
            observation_id=observation_id,
            parent_observation_id=parent_observation_id,
            branch_id=scope.branch_id,
            occurred_at=self._clock(),
        )
        if self.trace_index is not None:
            try:
                self.trace_index.update_event(event)
            except Exception as error:
                self._failure("index", error, trace_id=trace_id, scope=scope)
        return event

    def _failure(self, operation: str, error: Any, *, trace_id: str | None = None, scope: TraceScope | None = None) -> None:
        try:
            message = str(error)[:512]
        except Exception:
            message = type(error).__name__
        self.diagnostics.append({"operation": operation, "error": message})
        if trace_id is not None:
            self._failed_traces.add(trace_id)
        if self._failure_reported:
            return
        self._failure_reported = True
        # A session diagnostic without its branch would masquerade as global.
        if scope is None:
            return
        try:
            self.journal.append(
                "observability.failed",
                {"operation": operation, "error": {"type": type(error).__name__, "message": message}},
                session_id=scope.session_id,
                trace_id=trace_id,
                branch_id=scope.branch_id,
            )
        except Exception:
            pass


def _structured_error(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return json_safe(value)
    try:
        message = str(value)
    except Exception:
        message = "unavailable"
    return {"type": type(value).__name__, "message": message}


__all__ = ["JournalTraceRecorder"]
