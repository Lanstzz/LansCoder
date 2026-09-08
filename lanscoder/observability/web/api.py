"""Stable read-only presentation queries for the local Observatory."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from lanscoder.journal import JournalCorruptError, JournalStore
from lanscoder.observability.index import JournalTraceIndex
from lanscoder.observability.evidence import METADATA_FIELDS
from lanscoder.observability.models import Observation, TraceRecord, TraceSummary, project_trace
from lanscoder.session.branch import build_branch_topology, event_data, event_kind, event_sequence
from lanscoder.session.projection import project_branch
from lanscoder.storage import LansCoderPaths, PayloadIntegrityError, PayloadRef, PayloadStore


class ObservatoryProblem(ValueError):
    """A public, JSON-safe error from the read-only Observatory boundary."""

    def __init__(self, code: str, message: str, *, status: int, resource: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.resource = dict(resource or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "resource": self.resource}}


class ObservatoryNotFound(ObservatoryProblem):
    """Raised when an Observatory resource is absent."""

    def __init__(self, message: str, *, resource: Mapping[str, Any] | None = None) -> None:
        super().__init__("not_found", message, status=404, resource=resource)


_TRACE_FILTERS = frozenset({"project", "session_id", "status", "model", "provider", "tool", "tag"})
_TRACE_SINGLE = frozenset(
    {
        "from",
        "to",
        "has_error",
        "min_duration_ms",
        "max_duration_ms",
        "min_tokens",
        "max_tokens",
        "min_observation_count",
        "max_observation_count",
        "limit",
        "cursor",
    }
)
_OBSERVATION_STATUSES = frozenset({"succeeded", "failed", "cancelled", "skipped", "scheduled"})


class ObservatoryQueryService:
    """Project stable web DTOs from journals without exposing legacy event data."""

    def __init__(self, paths: LansCoderPaths | str | Path) -> None:
        self.paths = paths if isinstance(paths, LansCoderPaths) else LansCoderPaths(storage_root=paths)
        self.payloads = PayloadStore(self.paths)
        self.trace_index = JournalTraceIndex(self.paths, payload_store=self.payloads)

    def list_traces(self, query: Mapping[str, Sequence[str] | str]) -> dict[str, Any]:
        filters = _parse_trace_query(query)
        summaries, diagnostics = self._healthy_summaries()
        filtered = [summary for summary in summaries if _matches(summary, filters)]
        filtered.sort(key=lambda item: (item.started_at or "", item.trace_id), reverse=True)
        offset = _decode_cursor(filters["cursor"], filters["fingerprint"])
        page = filtered[offset : offset + filters["limit"]]
        next_cursor = None
        if offset + filters["limit"] < len(filtered):
            next_cursor = _encode_cursor(offset + filters["limit"], filters["fingerprint"])
        if filtered:
            empty_reason = None
        elif summaries:
            empty_reason = "filtered_empty"
        elif diagnostics:
            empty_reason = "corrupt"
        else:
            empty_reason = "no_traces"
        return {
            "items": [self._summary_dto(summary) for summary in page],
            "next_cursor": next_cursor,
            "total": len(filtered),
            "unfiltered_total": len(summaries),
            "empty_reason": empty_reason,
            "diagnostics": diagnostics,
        }

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        record, events = self._find_trace(trace_id)
        summary = record.to_summary()
        observations = _observation_dtos(record, events, self.payloads)
        relations = _relations(record, events)
        for relation in relations:
            parent = relation["parent_observation_id"]
            if parent is not None:
                for observation in observations:
                    if observation["observation_id"] == parent:
                        observation["relations"].append(relation)
                        break
        started = _trace_started_data(events, trace_id)
        trace_input = _descriptor_or_value(started.get("input"), self.payloads)
        final_output = _descriptor_or_value(record.output_ref or record.final_output, self.payloads)
        return {
            **self._summary_dto(summary),
            "parent_trace_id": record.parent_trace_id,
            "parent_observation_id": record.parent_observation_id,
            "outcome": record.outcome,
            "final_output": final_output,
            "error": record.error,
            "reason": record.reason,
            "input": trace_input,
            "metadata": dict(record.metadata),
            "relations": [relation for relation in relations if relation["parent_observation_id"] is None],
            "observations": observations,
            "evidence_completeness": {"complete": not record.incomplete, "incomplete": record.incomplete},
        }

    def list_sessions(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        for session_id in self._session_ids():
            try:
                events = self._session_events(session_id)
            except ObservatoryProblem as error:
                diagnostics.append(_diagnostic(session_id, error.message))
                continue
            created = _created_event(events)
            if created is None:
                diagnostics.append(_diagnostic(session_id, "session.created is missing"))
                continue
            data = event_data(created)
            if data.get("kind") != "primary":
                continue
            items.append(_session_dto(session_id, events, data))
        items.sort(key=lambda item: (item["updated_at"] or "", item["session_id"]), reverse=True)
        return {"items": items, "diagnostics": diagnostics}

    def replay_session(self, session_id: str, branch_id: str | None = None) -> dict[str, Any]:
        events = self._session_events(session_id)
        created = _created_event(events)
        if created is None or event_data(created).get("kind") != "primary":
            raise ObservatoryNotFound("primary session not found", resource={"session_id": session_id})
        try:
            topology = build_branch_topology(events)
        except ValueError as error:
            raise ObservatoryProblem("journal_corrupt", str(error), status=409, resource={"session_id": session_id}) from error
        selected = branch_id or topology.active_branch_id
        if selected not in topology.branches:
            raise ObservatoryNotFound("branch not found", resource={"session_id": session_id, "branch_id": selected})
        projected = project_branch(events, topology, selected)
        items, raw_events = _replay_items(projected)
        branches = [
            {
                "branch_id": node.branch_id,
                "parent_branch_id": node.parent_branch_id,
                "base_sequence": node.base_sequence,
                "active": node.branch_id == topology.active_branch_id,
            }
            for node in sorted(topology.branches.values(), key=lambda node: node.branch_id)
        ]
        return {
            "session_id": session_id,
            "active_branch_id": topology.active_branch_id,
            "selected_branch_id": selected,
            "root_branch_id": topology.root_branch_id,
            "branches": branches,
            "items": items,
            "raw_events": raw_events,
            "linked_trace_ids": _linked_trace_ids(events),
        }

    def read_payload(self, sha256: str, size_bytes: str | None) -> tuple[bytes, str]:
        if not _is_digest(sha256):
            raise ObservatoryNotFound("payload not found", resource={"sha256": sha256})
        size = _non_negative_int(size_bytes, "size_bytes")
        media_type = self._payload_media_type(sha256, size)
        if media_type is None:
            media_type = "application/octet-stream"
        try:
            data = self.payloads.read(PayloadRef(sha256, media_type, size))
        except FileNotFoundError as error:
            raise ObservatoryProblem("payload_missing", "payload is missing", status=404, resource={"sha256": sha256}) from error
        except (OSError, PayloadIntegrityError, ValueError) as error:
            raise ObservatoryProblem("payload_corrupt", "payload integrity check failed", status=409, resource={"sha256": sha256}) from error
        return data, media_type

    def _find_trace(self, trace_id: str) -> tuple[TraceRecord, list[Any]]:
        for session_id in self._session_ids():
            try:
                events = self._session_events(session_id)
            except ObservatoryProblem:
                continue
            if any(event_kind(event) == "trace.started" and getattr(event, "trace_id", None) == trace_id for event in events):
                return project_trace(events, trace_id, payload_store=self.payloads), events
        raise ObservatoryNotFound("trace not found", resource={"trace_id": trace_id})

    def _healthy_summaries(self) -> tuple[list[TraceSummary], list[dict[str, Any]]]:
        try:
            return self.trace_index.list_summaries(), []
        except (JournalCorruptError, OSError, RuntimeError, ValueError) as error:
            summaries, diagnostics = self._journal_summaries()
            diagnostics.insert(0, _index_diagnostic(str(error)))
            return summaries, diagnostics

    def _journal_summaries(self) -> tuple[list[TraceSummary], list[dict[str, Any]]]:
        summaries: list[TraceSummary] = []
        diagnostics: list[dict[str, Any]] = []
        for session_id in self._session_ids():
            try:
                events = self._session_events(session_id)
            except ObservatoryProblem as error:
                diagnostics.append(_diagnostic(session_id, error.message))
                continue
            for trace_id in sorted({event.trace_id for event in events if event_kind(event) == "trace.started" and event.trace_id}):
                try:
                    summaries.append(project_trace(events, trace_id, payload_store=self.payloads).to_summary())
                except (ValueError, OSError) as error:
                    diagnostics.append(_diagnostic(session_id, str(error), trace_id=trace_id))
        return summaries, diagnostics

    def _summary_dto(self, summary: TraceSummary) -> dict[str, Any]:
        events = self._session_events(summary.session_id)
        started = _trace_started_data(events, summary.trace_id)
        preview = _preview(started.get("input"))
        try:
            topology = build_branch_topology(events)
            branch_state = "active" if summary.branch_id == topology.active_branch_id else "historical"
        except ValueError:
            branch_state = "historical"
        return {
            **summary.to_dict(),
            "input_preview": preview,
            "input_preview_truncated": _preview_truncated(started.get("input")),
            "branch_state": branch_state,
            "detached": _is_detached(events, summary.trace_id),
            "evidence": "incomplete" if summary.incomplete else "complete",
        }

    def _session_ids(self) -> list[str]:
        return sorted(path.stem for path in self.paths.sessions.glob("*.jsonl"))

    def _session_events(self, session_id: str) -> list[Any]:
        try:
            events = JournalStore(self.paths, session_id).read_events()
        except JournalCorruptError as error:
            raise ObservatoryProblem("journal_corrupt", str(error), status=409, resource={"session_id": session_id}) from error
        if not events:
            raise ObservatoryNotFound("session not found", resource={"session_id": session_id})
        return events

    def _payload_media_type(self, sha256: str, size_bytes: int) -> str | None:
        for session_id in self._session_ids():
            try:
                events = self._session_events(session_id)
            except ObservatoryProblem:
                continue
            for event in events:
                for reference in _payload_refs(event_data(event)):
                    if reference.get("sha256") == sha256 and reference.get("size_bytes") == size_bytes:
                        media_type = reference.get("media_type")
                        if isinstance(media_type, str) and media_type:
                            return media_type
        return None


def _parse_trace_query(query: Mapping[str, Sequence[str] | str]) -> dict[str, Any]:
    normalized = {key: _values(value) for key, value in query.items()}
    for key, values in normalized.items():
        if key not in _TRACE_FILTERS and key not in _TRACE_SINGLE and not key.startswith("metadata."):
            raise ObservatoryProblem("invalid_query", f"unknown query parameter: {key}", status=400)
        if key in _TRACE_SINGLE and len(values) != 1:
            raise ObservatoryProblem("invalid_query", f"query parameter must occur once: {key}", status=400)
    for name in ("from", "to"):
        if name in normalized:
            _parse_rfc3339(normalized[name][0])
    boolean = normalized.get("has_error", [None])[0]
    if boolean not in {None, "true", "false"}:
        raise ObservatoryProblem("invalid_query", "has_error must be true or false", status=400)
    if any(value not in {"running", "waiting_for_input", "completed", "failed", "cancelled"} for value in normalized.get("status", [])):
        raise ObservatoryProblem("invalid_query", "status is invalid", status=400)
    numeric = {name: _non_negative_int(normalized[name][0], name) for name in _TRACE_SINGLE - {"from", "to", "has_error", "cursor"} if name in normalized}
    limit = numeric.get("limit", 50)
    if not 1 <= limit <= 200:
        raise ObservatoryProblem("invalid_query", "limit must be between 1 and 200", status=400)
    metadata: dict[str, list[Any]] = {}
    for key, values in normalized.items():
        if key.startswith("metadata."):
            name = key.removeprefix("metadata.")
            if not name:
                raise ObservatoryProblem("invalid_query", "metadata key is required", status=400)
            if name not in METADATA_FIELDS:
                raise ObservatoryProblem("invalid_query", f"metadata key is not queryable: {name}", status=400)
            parsed_values: list[Any] = []
            for raw_value in values:
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError as error:
                    raise ObservatoryProblem("invalid_query", "metadata value must be a JSON scalar", status=400) from error
                if isinstance(value, (dict, list)) or isinstance(value, float) and not math.isfinite(value):
                    raise ObservatoryProblem("invalid_query", "metadata value must be a JSON scalar", status=400)
                parsed_values.append(value)
            metadata[name] = parsed_values
    canonical = {key: sorted(values) for key, values in normalized.items() if key not in {"cursor", "limit"}}
    fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"values": normalized, "metadata": metadata, "limit": limit, "cursor": normalized.get("cursor", [None])[0], "fingerprint": fingerprint, **numeric}


def _matches(summary: TraceSummary, filters: Mapping[str, Any]) -> bool:
    values = filters["values"]
    actual = {
        "project": summary.metadata.get("project_id"),
        "session_id": summary.session_id,
        "status": summary.status.value,
        "model": summary.model,
        "provider": summary.provider,
        "tool": summary.tool_name,
    }
    for key, value in actual.items():
        expected = values.get(key, [])
        if expected and value not in expected:
            return False
    if values.get("tag") and not any(tag in summary.tags for tag in values["tag"]):
        return False
    if "has_error" in values and summary.has_error != (values["has_error"][0] == "true"):
        return False
    if "from" in values and (summary.started_at is None or _parse_rfc3339(summary.started_at) < _parse_rfc3339(values["from"][0])):
        return False
    if "to" in values and (summary.started_at is None or _parse_rfc3339(summary.started_at) > _parse_rfc3339(values["to"][0])):
        return False
    ranges = (("duration_ms", summary.duration_ms), ("tokens", summary.total_tokens), ("observation_count", summary.observation_count))
    for name, actual_value in ranges:
        minimum = filters.get(f"min_{name}")
        maximum = filters.get(f"max_{name}")
        if (minimum is not None or maximum is not None) and actual_value is None:
            return False
        if minimum is not None and actual_value < minimum:
            return False
        if maximum is not None and actual_value > maximum:
            return False
    return all(any(_json_scalar_equal(summary.metadata.get(key), expected_value) for expected_value in expected) for key, expected in filters["metadata"].items())


def _observation_dtos(record: TraceRecord, events: Sequence[Any], payloads: PayloadStore) -> list[dict[str, Any]]:
    started_events = {event.observation_id: event for event in events if event_kind(event) == "observation.started" and event.trace_id == record.trace_id}
    ended_events = {event.observation_id: event for event in events if event_kind(event) == "observation.ended" and event.trace_id == record.trace_id}
    root_started = _parse_rfc3339(record.started_at) if record.started_at else None
    result: list[dict[str, Any]] = []
    for observation in record.observations:
        data = observation.data
        diagnostics = _observation_diagnostics(observation, payloads)
        event_category = data.get("event") if observation.observation_type.value == "event" and isinstance(data.get("event"), str) else None
        status = observation.outcome if observation.outcome in _OBSERVATION_STATUSES else "running" if observation.ended_at is None else "unknown"
        input_value, output_value, overview = _observation_fields(observation, payloads)
        start_offset = None
        if root_started is not None and observation.started_at is not None:
            start_offset = int((_parse_rfc3339(observation.started_at) - root_started).total_seconds() * 1000)
        result.append(
            {
                "observation_id": observation.observation_id,
                "type": observation.observation_type.value,
                "display_name": _display_name(observation, event_category),
                "status": status,
                "outcome": observation.outcome,
                "event_category": event_category,
                "started_at": observation.started_at,
                "ended_at": observation.ended_at,
                "start_offset_ms": start_offset,
                "duration_ms": observation.duration_ms,
                "depth": 0,
                "parent_observation_id": observation.parent_observation_id,
                "incomplete": _is_observation_incomplete(diagnostics),
                "diagnostics": diagnostics,
                "error": observation.error,
                "input": input_value,
                "output": output_value,
                "metadata": dict(data.get("metadata", {})) if isinstance(data.get("metadata"), Mapping) else {},
                "overview": overview,
                "relations": [],
                "raw": {
                    "started_event": _raw_event(started_events.get(observation.observation_id)),
                    "ended_event": _raw_event(ended_events.get(observation.observation_id)),
                },
            }
        )
    _assign_depths(result)
    return result


def _observation_fields(observation: Observation, payloads: PayloadStore) -> tuple[Any, Any, dict[str, Any]]:
    data = observation.data
    kind = observation.observation_type.value
    if kind == "generation":
        return (
            _descriptor_or_value(data.get("normalized_request"), payloads),
            _descriptor_or_value(data.get("normalized_response"), payloads),
            {key: data.get(key) for key in ("model", "provider", "parameters", "usage", "usage_details", "stream_summary", "finish_reason") if key in data},
        )
    if kind == "tool":
        output = {key: data[key] for key in ("result", "ok", "result_type") if key in data} or None
        return data.get("arguments"), output, {key: data.get(key) for key in ("tool_name", "arguments", "result", "outcome", "error") if key in data}
    if kind == "event":
        allowed = ("event", "tool_call_id", "tool_name", "permission_request_id", "permission_decision", "prewrite_review", "request_id", "job_id", "status")
        return None, None, {key: data.get(key) for key in allowed if key in data}
    return None, None, {key: data.get(key) for key in ("turn", "limits") if key in data}


def _descriptor_or_value(value: Any, payloads: PayloadStore) -> Any:
    reference = _payload_ref(value)
    if reference is None:
        return value
    try:
        payloads.read(reference)
        availability = "available"
    except FileNotFoundError:
        availability = "missing"
    except (OSError, PayloadIntegrityError, ValueError):
        availability = "corrupt"
    preview = "json" if reference.media_type == "application/json" or reference.media_type.endswith("+json") else "text" if reference.media_type.startswith("text/") else "metadata_only"
    return {
        "sha256": reference.sha256,
        "media_type": reference.media_type,
        "size_bytes": reference.size_bytes,
        "url": f"/api/v1/payloads/{reference.sha256}?{urlencode({'size_bytes': reference.size_bytes})}",
        "availability": availability,
        "preview": preview,
    }


def _relations(record: TraceRecord, events: Sequence[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for link in record.links:
        linked = link.get("child_trace_id") if link.get("parent_trace_id") == record.trace_id else link.get("parent_trace_id")
        if not isinstance(linked, str):
            continue
        relation = _relation(link.get("relation"), linked, link)
        result.append(relation)
    for event in events:
        if event_kind(event) != "background.scheduled":
            continue
        scheduled = event_data(event)
        if scheduled.get("parent_trace_id") != record.trace_id:
            continue
        job_id = scheduled.get("job_id")
        if not isinstance(job_id, str):
            continue
        completion = _background_completion(events, job_id)
        background_trace_id = completion.get("background_trace_id") if completion is not None else None
        relation = next(
            (item for item in result if item["relation"] == "background" and item["job_id"] == job_id),
            None,
        )
        if relation is None:
            relation = _relation("background", background_trace_id, {"job_id": job_id})
            result.append(relation)
        if relation["linked_trace_id"] is None and isinstance(background_trace_id, str):
            relation["linked_trace_id"] = background_trace_id
        parent_observation_id = scheduled.get("parent_observation_id")
        if isinstance(parent_observation_id, str):
            relation["parent_observation_id"] = parent_observation_id
        relation["dispatch_status"] = scheduled.get("status", "scheduled")
        if completion is not None:
            relation["completion_status"] = _completion_status(completion)
            relation["detached"] = completion.get("detached_from_active_branch") is True
    return result


def _relation(relation: Any, linked_trace_id: str | None, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "relation": relation if isinstance(relation, str) else "linked",
        "linked_trace_id": linked_trace_id,
        "parent_observation_id": data.get("parent_observation_id"),
        "job_id": data.get("job_id"),
        "dispatch_status": data.get("dispatch_status"),
        "completion_status": data.get("completion_status"),
        "detached": False,
    }


def _is_detached(events: Sequence[Any], trace_id: str) -> bool:
    scheduled_jobs = {data.get("job_id") for event in events if event_kind(event) == "background.scheduled" for data in [event_data(event)] if isinstance(data.get("job_id"), str)}
    for event in events:
        if event_kind(event) in {"background.completed", "background.failed", "background.cancelled"}:
            data = event_data(event)
            if data.get("job_id") in scheduled_jobs and data.get("background_trace_id") == trace_id and data.get("detached_from_active_branch") is True:
                return True
    return False


def _background_completion(events: Sequence[Any], job_id: str) -> dict[str, Any] | None:
    scheduled = any(event_kind(event) == "background.scheduled" and event_data(event).get("job_id") == job_id for event in events)
    if not scheduled:
        return None
    return next(
        (event_data(event) for event in events if event_kind(event) in {"background.completed", "background.failed", "background.cancelled"} and event_data(event).get("job_id") == job_id),
        None,
    )


def _completion_status(completion: Mapping[str, Any]) -> Any:
    if "status" in completion:
        return completion["status"]
    return completion.get("outcome")


def _replay_items(events: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    raw_events: list[dict[str, Any]] = []
    for fallback, event in enumerate(events, start=1):
        data = event_data(event)
        if event_kind(event) == "message.appended" and data.get("role") in {"user", "assistant", "notification"}:
            content = _message_content(data)
            if not isinstance(content, str):
                raw_events.append(_raw_event(event))
                continue
            trace_id = data.get("trace_id") if isinstance(data.get("trace_id"), str) else None
            items.append(
                {
                    "sequence": event_sequence(event, fallback),
                    "role": data["role"],
                    "content": content,
                    "trace_id": trace_id,
                    "branch_id": event.branch_id,
                    "status": _notification_status(data, events) if data["role"] == "notification" else "recorded",
                    "linked_trace_ids": _notification_linked_trace_ids(data, events) if data["role"] == "notification" else _linked_trace_ids([event] + list(events)),
                }
            )
        elif event_kind(event) != "session.created":
            raw_events.append(_raw_event(event))
    return items, raw_events


def _notification_status(data: Mapping[str, Any], events: Sequence[Any]) -> str:
    job_id = _notification_job_id(data)
    for event in events:
        if event_kind(event) in {"background.completed", "background.failed", "background.cancelled"} and event_data(event).get("job_id") == job_id:
            return str(event_data(event).get("status") or event_data(event).get("outcome") or data.get("background_status") or "recorded")
    return str(data.get("background_status") or "recorded")


def _notification_linked_trace_ids(data: Mapping[str, Any], events: Sequence[Any]) -> list[str]:
    """Return only traces causally associated with one notification job."""
    job_id = _notification_job_id(data)
    if not isinstance(job_id, str) or not job_id:
        return []
    linked: set[str] = set()
    for event in events:
        if event_kind(event) not in {"background.scheduled", "background.completed", "background.failed", "background.cancelled"}:
            continue
        event_data_value = event_data(event)
        if event_data_value.get("job_id") != job_id:
            continue
        if isinstance(getattr(event, "trace_id", None), str) and event.trace_id:
            linked.add(event.trace_id)
        _collect_trace_ids(event_data_value, linked)
    for event in events:
        if event_kind(event) != "trace.linked":
            continue
        link_data = event_data(event)
        if link_data.get("job_id") == job_id:
            _collect_trace_ids(link_data, linked)
    return sorted(linked)


def _notification_job_id(data: Mapping[str, Any]) -> str | None:
    metadata = data.get("parts", [{}])[0].get("metadata", {}) if isinstance(data.get("parts"), list) and data.get("parts") else {}
    return metadata.get("background_job_id") if isinstance(metadata, Mapping) else data.get("job_id")


def _session_dto(session_id: str, events: Sequence[Any], created_data: Mapping[str, Any]) -> dict[str, Any]:
    messages = [event for event in events if event_kind(event) == "message.appended"]
    latest_user = next((_message_content(event_data(event)) for event in reversed(messages) if event_data(event).get("role") == "user"), None)
    return {
        "session_id": session_id,
        "title": created_data.get("title") or session_id,
        "latest_user_input": latest_user if isinstance(latest_user, str) else None,
        "updated_at": events[-1].occurred_at,
        "message_count": len(messages),
        "status": "ok",
    }


def _observation_diagnostics(observation: Observation, payloads: PayloadStore) -> list[str]:
    result: list[str] = []
    if observation.ended_at is None:
        result.append("unfinished")
    if observation.data.get("evidence_incomplete"):
        result.append("evidence_incomplete")
    for reference in _payload_refs(observation.data):
        try:
            payloads.read(PayloadRef.from_dict(reference))
        except FileNotFoundError:
            result.append("payload_missing")
        except (OSError, PayloadIntegrityError, ValueError):
            result.append("payload_corrupt")
    return list(dict.fromkeys(result))


def _assign_depths(observations: list[dict[str, Any]]) -> None:
    by_id = {item["observation_id"]: item for item in observations}
    for item in observations:
        seen = {item["observation_id"]}
        parent = item["parent_observation_id"]
        depth = 0
        while parent is not None:
            if parent in seen:
                item["diagnostics"].append("parent_cycle")
                item["parent_observation_id"] = None
                break
            seen.add(parent)
            parent_item = by_id.get(parent)
            if parent_item is None:
                item["diagnostics"].append("orphan_parent")
                item["parent_observation_id"] = None
                break
            depth += 1
            parent = parent_item["parent_observation_id"]
        item["depth"] = depth
        item["incomplete"] = _is_observation_incomplete(item["diagnostics"])


def _display_name(observation: Observation, event_category: str | None) -> str:
    data = observation.data
    if observation.observation_type.value == "generation":
        return f"Generation · {data['model']}" if isinstance(data.get("model"), str) else "Generation"
    if observation.observation_type.value == "tool":
        return f"Tool · {data['tool_name']}" if isinstance(data.get("tool_name"), str) else "Tool"
    if observation.observation_type.value == "event":
        return f"Event · {event_category}" if event_category else "Event"
    return "Agent turn"


def _raw_event(event: Any | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {"event_id": event.event_id, "sequence": event.sequence, "occurred_at": event.occurred_at, "data": event_data(event)}


def _trace_started_data(events: Sequence[Any], trace_id: str) -> dict[str, Any]:
    event = next((event for event in events if event_kind(event) == "trace.started" and event.trace_id == trace_id), None)
    return event_data(event) if event is not None else {}


def _created_event(events: Sequence[Any]) -> Any | None:
    return next((event for event in events if event_kind(event) == "session.created"), None)


def _linked_trace_ids(events: Sequence[Any]) -> list[str]:
    result = {event.trace_id for event in events if isinstance(getattr(event, "trace_id", None), str) and event.trace_id}
    for event in events:
        if event_kind(event) == "trace.linked":
            for reference in _payload_refs(event_data(event)):
                result.discard(reference.get("sha256"))
            _collect_trace_ids(event_data(event), result)
        elif event_kind(event) in {"background.scheduled", "background.completed", "background.failed", "background.cancelled"}:
            background_trace_id = event_data(event).get("background_trace_id")
            if isinstance(background_trace_id, str) and background_trace_id:
                result.add(background_trace_id)
    return sorted(result)


def _collect_trace_ids(value: Any, result: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "trace_id" or key.endswith("_trace_id") or key.endswith("_trace_ids"):
                if isinstance(item, str):
                    result.add(item)
                elif isinstance(item, list):
                    result.update(item for item in item if isinstance(item, str))
            elif isinstance(item, (Mapping, list)):
                _collect_trace_ids(item, result)
    elif isinstance(value, list):
        for item in value:
            _collect_trace_ids(item, result)


def _payload_refs(value: Any):
    if isinstance(value, Mapping):
        if {"sha256", "media_type", "size_bytes"}.issubset(value):
            yield dict(value)
        else:
            for item in value.values():
                yield from _payload_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _payload_refs(item)


def _payload_ref(value: Any) -> PayloadRef | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("payload_ref", value)
    if not isinstance(raw, Mapping) or not {"sha256", "media_type", "size_bytes"}.issubset(raw):
        return None
    try:
        return PayloadRef.from_dict(dict(raw))
    except (TypeError, ValueError):
        return None


def _preview(value: Any) -> str | None:
    if isinstance(value, str):
        return value[:240]
    return None


def _preview_truncated(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 240


def _message_content(data: Mapping[str, Any]) -> str | None:
    if isinstance(data.get("content"), str):
        return data["content"]
    parts = data.get("parts")
    if not isinstance(parts, list):
        return None
    text = [part.get("content") for part in parts if isinstance(part, Mapping) and part.get("kind") == "text" and isinstance(part.get("content"), str)]
    return "\n".join(text) if text else None


def _values(value: Sequence[str] | str) -> list[str]:
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else [str(value)]


def _parse_rfc3339(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ObservatoryProblem("invalid_query", "time must be RFC3339", status=400) from error
    if parsed.tzinfo is None:
        raise ObservatoryProblem("invalid_query", "time must include UTC offset", status=400)
    return parsed.astimezone(UTC)


def _non_negative_int(value: str | None, name: str) -> int:
    try:
        parsed = int(value) if value is not None else -1
    except ValueError as error:
        raise ObservatoryProblem("invalid_query", f"{name} must be a non-negative integer", status=400) from error
    if parsed < 0:
        raise ObservatoryProblem("invalid_query", f"{name} must be a non-negative integer", status=400)
    return parsed


def _encode_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"offset": offset, "fingerprint": fingerprint}, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str | None, fingerprint: str) -> int:
    if value is None:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(decoded)
        offset = payload["offset"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, base64.binascii.Error) as error:
        raise ObservatoryProblem("invalid_cursor", "cursor is invalid", status=400) from error
    if payload.get("fingerprint") != fingerprint or not isinstance(offset, int) or offset < 0:
        raise ObservatoryProblem("invalid_cursor", "cursor does not match this query", status=400)
    return offset


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _json_scalar_equal(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


def _is_observation_incomplete(diagnostics: Sequence[str]) -> bool:
    return any(code in {"unfinished", "evidence_incomplete", "payload_missing", "payload_corrupt"} for code in diagnostics)


def _diagnostic(session_id: str, message: str, *, trace_id: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": "journal_corrupt", "session_id": session_id, "message": message}
    if trace_id is not None:
        result["trace_id"] = trace_id
    return result


def _index_diagnostic(message: str) -> dict[str, Any]:
    return {"code": "trace_index_unavailable", "message": message}


def parse_query(raw_query: str) -> dict[str, list[str]]:
    return {key: values for key, values in parse_qs(raw_query, keep_blank_values=True).items()}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
