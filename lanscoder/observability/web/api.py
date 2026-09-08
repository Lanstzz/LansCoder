"""Read-only queries used by the local Observatory web boundary."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from lanscoder.journal import JournalStore
from lanscoder.observability.index import JournalTraceIndex
from lanscoder.observability.models import Observation, TraceRecord, TraceSummary, project_trace
from lanscoder.session.branch import build_branch_topology, event_data, event_kind
from lanscoder.session.index import SessionIndex
from lanscoder.storage import LansCoderPaths, PayloadRef, PayloadStore


class ObservatoryNotFound(LookupError):
    """Raised when a requested read-only Observatory resource does not exist."""


class ObservatoryQueryService:
    """Project traces, sessions, replay data, and payloads from local storage."""

    def __init__(self, paths: LansCoderPaths | str | Path) -> None:
        self.paths = paths if isinstance(paths, LansCoderPaths) else LansCoderPaths(storage_root=paths)
        self.payloads = PayloadStore(self.paths)
        self.trace_index = JournalTraceIndex(self.paths, payload_store=self.payloads)

    def list_traces(self, query: Mapping[str, Sequence[str] | str]) -> dict[str, Any]:
        summaries = self._trace_summaries()
        filtered = [summary for summary in summaries if _matches(summary, query)]
        filtered.sort(key=lambda item: (item.started_at or "", item.trace_id), reverse=True)
        limit = _int_query(query, "limit", default=50, minimum=1, maximum=200)
        offset = _decode_cursor(_first(query, "cursor"))
        page = filtered[offset : offset + limit]
        next_cursor = _encode_cursor(offset + limit) if offset + limit < len(filtered) else None
        return {"items": [item.to_dict() for item in page], "next_cursor": next_cursor, "total": len(filtered)}

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        record = self._find_trace(trace_id)
        observations = [_observation_dict(item) for item in record.observations]
        tree = _observation_tree(observations)
        timeline = sorted(observations, key=lambda item: (item.get("started_at") or "", item["observation_id"]))
        summary = self._summary_for_record(record).to_dict()
        events = self._session_events(record.session_id)
        started_data = next(
            (event_data(event) for event in events if event_kind(event) == "trace.started" and getattr(event, "trace_id", None) == trace_id),
            {},
        )
        evidence_payloads = _resolve_payloads(events, trace_id, self.payloads)
        result = {
            **summary,
            "parent_trace_id": record.parent_trace_id,
            "parent_observation_id": record.parent_observation_id,
            "outcome": record.outcome,
            "final_output": record.final_output,
            "output_ref": record.output_ref,
            "error": record.error,
            "reason": record.reason,
            "input": started_data.get("input"),
            "trace_metadata": dict(started_data.get("metadata", {})) if isinstance(started_data.get("metadata"), Mapping) else {},
            "evidence_payloads": evidence_payloads,
            "model_parameters": dict(summary.get("parameters", {})),
            "usage_details": dict(summary.get("usage_details", {})),
            "stream_summary": _stream_summary(record.observations),
            "observations": observations,
            "observation_tree": tree,
            "timeline": timeline,
            "links": list(record.links),
            "branch_links": [link for link in record.links if link.get("relation") in {"child", "parent", "background", "subagent"}],
            "rewind_links": [link for link in record.links if link.get("relation") == "rewind_from"],
            "evidence_completeness": {"complete": not record.incomplete, "incomplete": record.incomplete},
        }
        return result

    def list_sessions(self) -> dict[str, Any]:
        records = SessionIndex(self.paths.storage_root).list_records(project_id=None, kind="primary")
        return {"items": [_session_dict(record) for record in records]}

    def replay_session(self, session_id: str) -> dict[str, Any]:
        events = self._session_events(session_id)
        created = next((event for event in events if event_kind(event) == "session.created"), None)
        if created is None or event_data(created).get("kind", "primary") != "primary":
            raise ObservatoryNotFound(f"primary session not found: {session_id}")
        topology = build_branch_topology(events)
        branches = []
        for branch_id, node in sorted(topology.branches.items()):
            branch_events = [_event_dict(event) for event in events if event_kind(event) != "session.recalled" and getattr(event, "branch_id", None) == branch_id]
            branches.append(
                {
                    "branch_id": branch_id,
                    "parent_branch_id": node.parent_branch_id,
                    "base_sequence": node.base_sequence,
                    "active": branch_id == topology.active_branch_id,
                    "events": branch_events,
                }
            )
        linked_trace_ids = _linked_trace_ids(events)
        return {
            "session_id": session_id,
            "active_branch_id": topology.active_branch_id,
            "root_branch_id": topology.root_branch_id,
            "branches": branches,
            "linked_trace_ids": linked_trace_ids,
        }

    def read_payload(self, sha256: str) -> tuple[bytes, str]:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ObservatoryNotFound("invalid payload digest")
        try:
            data = self.payloads.read(sha256)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise ObservatoryNotFound(f"payload not found: {sha256}") from error
        media_type = self._payload_media_type(sha256) or "application/octet-stream"
        return data, media_type

    def _find_trace(self, trace_id: str) -> TraceRecord:
        for session_id in self._session_ids():
            events = self._session_events(session_id)
            if any(event_kind(event) == "trace.started" and getattr(event, "trace_id", None) == trace_id for event in events):
                return project_trace(events, trace_id, payload_store=self.payloads)
        raise ObservatoryNotFound(f"trace not found: {trace_id}")

    def _trace_summaries(self) -> list[TraceSummary]:
        summaries = self.trace_index.list_summaries()
        enriched: list[TraceSummary] = []
        for summary in summaries:
            if "project_id" in summary.metadata:
                enriched.append(summary)
                continue
            started = next(
                (event for event in self._session_events(summary.session_id) if event_kind(event) == "trace.started" and getattr(event, "trace_id", None) == summary.trace_id),
                None,
            )
            project_id = event_data(started).get("project_id") if started is not None else None
            metadata = dict(summary.metadata)
            if isinstance(project_id, str) and project_id:
                metadata["project_id"] = project_id
            enriched.append(replace(summary, metadata=metadata))
        return enriched

    def _summary_for_record(self, record: TraceRecord) -> TraceSummary:
        summary = record.to_summary()
        if "project_id" in summary.metadata:
            return summary
        events = self._session_events(record.session_id)
        started = next((event for event in events if event_kind(event) == "trace.started" and getattr(event, "trace_id", None) == record.trace_id), None)
        project_id = event_data(started).get("project_id") if started is not None else None
        if not isinstance(project_id, str) or not project_id:
            return summary
        metadata = {**summary.metadata, "project_id": project_id}
        return replace(summary, metadata=metadata)

    def _session_ids(self) -> list[str]:
        return sorted(path.stem for path in self.paths.sessions.glob("*.jsonl"))

    def _session_events(self, session_id: str) -> list[Any]:
        try:
            return JournalStore(self.paths, session_id).read_events()
        except FileNotFoundError as error:
            raise ObservatoryNotFound(f"session not found: {session_id}") from error

    def _payload_media_type(self, sha256: str) -> str | None:
        for session_id in self._session_ids():
            for event in self._session_events(session_id):
                for reference in _payload_refs(event_data(event)):
                    if reference.get("sha256") == sha256:
                        value = reference.get("media_type")
                        if isinstance(value, str) and value:
                            return value
        return None


def _matches(summary: TraceSummary, query: Mapping[str, Sequence[str] | str]) -> bool:
    checks = {
        "session_id": summary.session_id,
        "status": summary.status.value,
        "model": summary.model,
        "provider": summary.provider,
        "tool": summary.tool_name,
        "tool_name": summary.tool_name,
        "project": summary.metadata.get("project") or summary.metadata.get("project_id"),
        "project_id": summary.metadata.get("project_id"),
    }
    for name, actual in checks.items():
        expected = _first(query, name)
        if expected and (actual or "") != expected:
            return False
    has_error = _first(query, "has_error")
    if has_error is not None and (summary.has_error != _parse_bool(has_error)):
        return False
    for name, actual in (
        ("min_duration", summary.duration_ms),
        ("max_duration", summary.duration_ms),
        ("min_tokens", summary.total_tokens),
        ("max_tokens", summary.total_tokens),
        ("min_observations", summary.observation_count),
        ("max_observations", summary.observation_count),
    ):
        expected = _first(query, name)
        if expected is None or actual is None:
            continue
        threshold = _int_query(query, name, default=0)
        if name.startswith("min_") and actual < threshold:
            return False
        if name.startswith("max_") and actual > threshold:
            return False
    start = _first(query, "from") or _first(query, "start") or _first(query, "start_time") or _first(query, "from_time")
    end = _first(query, "to") or _first(query, "end") or _first(query, "end_time") or _first(query, "to_time")
    if start and (summary.started_at or "") < start:
        return False
    if end and (summary.started_at or "") > end:
        return False
    expected_tag = _first(query, "tag")
    if expected_tag and expected_tag not in summary.tags:
        return False
    for key, value in _query_items(query):
        if key.startswith("metadata.") and summary.metadata.get(key[9:]) != value:
            return False
        if key.startswith("tags.") and value not in summary.tags:
            return False
    return True


def _query_items(query: Mapping[str, Sequence[str] | str]):
    for key, raw in query.items():
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            yield key, str(value)


def _first(query: Mapping[str, Sequence[str] | str], name: str) -> str | None:
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value) if value is not None else None


def _int_query(query: Mapping[str, Sequence[str] | str], name: str, *, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(_first(query, name) or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _parse_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()))
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return 0


def _observation_dict(observation: Observation) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id,
        "trace_id": observation.trace_id,
        "session_id": observation.session_id,
        "branch_id": observation.branch_id,
        "observation_type": observation.observation_type.value,
        "parent_observation_id": observation.parent_observation_id,
        "started_at": observation.started_at,
        "ended_at": observation.ended_at,
        "duration_ms": observation.duration_ms,
        "outcome": observation.outcome,
        "error": observation.error,
        "data": observation.data,
    }


def _observation_tree(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = {item["observation_id"]: {**item, "children": []} for item in observations}
    roots = []
    for node in nodes.values():
        parent = nodes.get(node.get("parent_observation_id"))
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots


def _stream_summary(observations: Sequence[Observation]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for observation in observations:
        value = observation.data.get("stream_summary")
        if isinstance(value, Mapping):
            result.update(value)
    return result


def _session_dict(record: Any) -> dict[str, Any]:
    value = dict(record.__dict__) if hasattr(record, "__dict__") else {name: getattr(record, name) for name in record.__slots__}
    return value


def _event_dict(event: Any) -> dict[str, Any]:
    return event.to_dict() if hasattr(event, "to_dict") else dict(event)


def _linked_trace_ids(events: Sequence[Any]) -> list[str]:
    result = {event.trace_id for event in events if isinstance(getattr(event, "trace_id", None), str) and event.trace_id}
    for event in events:
        if event_kind(event) == "trace.linked":
            _collect_linked_trace_ids(event_data(event), result)
    return sorted(result)


def _collect_linked_trace_ids(value: Any, result: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and (key == "trace_id" or key.endswith("_trace_id") or key.endswith("_trace_ids")):
                _add_linked_trace_ids(item, result)
            elif isinstance(item, (Mapping, list, tuple)):
                _collect_linked_trace_ids(item, result)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_linked_trace_ids(item, result)


def _add_linked_trace_ids(value: Any, result: set[str]) -> None:
    if isinstance(value, str) and value:
        result.add(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _add_linked_trace_ids(item, result)
    elif isinstance(value, Mapping):
        _collect_linked_trace_ids(value, result)


def _payload_refs(value: Any):
    if isinstance(value, Mapping):
        if {"sha256", "media_type", "size_bytes"}.issubset(value):
            yield value
        else:
            for item in value.values():
                yield from _payload_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _payload_refs(item)


def _resolve_payloads(events: Sequence[Any], trace_id: str, payload_store: PayloadStore) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if getattr(event, "trace_id", None) != trace_id:
            continue
        for reference in _payload_refs(event_data(event)):
            digest = reference.get("sha256")
            if not isinstance(digest, str) or digest in seen:
                continue
            seen.add(digest)
            item: dict[str, Any] = {"ref": dict(reference), "resolved": False}
            try:
                raw = payload_store.read(PayloadRef.from_dict(dict(reference)))
                media_type = str(reference.get("media_type") or "")
                if media_type == "application/json" or media_type.endswith("+json"):
                    item["value"] = json.loads(raw.decode("utf-8"))
                else:
                    item["value"] = raw.decode("utf-8")
                item["resolved"] = True
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                item["value"] = None
            result.append(item)
    return result


def parse_query(raw_query: str) -> dict[str, list[str]]:
    return {key: values for key, values in parse_qs(raw_query, keep_blank_values=True).items()}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
