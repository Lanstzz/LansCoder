from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.catalog import SessionCatalog, require_usable_record
from lanscoder.session.errors import SessionNotFoundError
from lanscoder.session.models import ResumeResult
from lanscoder.session.projection import active_projection
from lanscoder.session.resume import validate_session_schema
from lanscoder.storage import LansCoderPaths
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess

_EVENT_LINK_FIELDS = {
    "checkpoint.created": {"session_id"},
    "message.appended": {"message_id"},
    "message.part.metadata.updated": {"message_id", "part_id"},
    "compaction.completed": {"checkpoint_id"},
    "llm.compaction.completed": {"checkpoint_id"},
    "trace.started": {"parent_trace_id", "parent_observation_id"},
    "trace.linked": {
        "parent_trace_id",
        "child_trace_id",
        "parent_session_id",
        "child_session_id",
        "parent_observation_id",
        "triggering_observation_id",
    },
    "background.scheduled": {"parent_trace_id", "parent_observation_id"},
    "background.completed": {"background_trace_id"},
    "background.failed": {"background_trace_id"},
    "background.cancelled": {"background_trace_id"},
}


@dataclass(slots=True)
class ForkSessionService:
    store: JsonlSessionStore
    project_root: str | Path
    paths: LansCoderPaths | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    catalog: SessionCatalog | None = None

    def fork(self, source_session_id: str, *, title: str | None = None) -> ResumeResult:
        paths = self.paths or LansCoderPaths(storage_root=self.store.root, project_root=self.project_root)
        validate_session_schema(self.store, source_session_id)
        catalog = self.catalog or SessionCatalog(self.store.root)
        bootstrap = SessionBootstrap(
            store=self.store,
            project_root=self.project_root,
            paths=paths,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        policy = bootstrap.access_policy()
        target = policy.fork_primary(source_session_id)
        record = require_usable_record(catalog.get_session(source_session_id))

        events = self.store.list_events(source_session_id)
        if not events:
            raise SessionNotFoundError(f"session not found: {source_session_id}")

        forked_session_id = target.session_id
        projected_events = active_projection(events)
        source_created = next((event for event in projected_events if event.kind == "session.created"), None)
        if source_created is None:
            raise SessionNotFoundError(f"session has no session.created event: {source_session_id}")

        metadata = dict(source_created.data)
        metadata.pop("session_id", None)
        metadata.pop("root_branch_id", None)
        metadata.update(target.metadata)
        metadata.pop("session_id", None)
        metadata.pop("root_branch_id", None)
        metadata["title"] = title or f"Fork of {record.title}"
        target_writer = SessionEventWriter(store=self.store, session_id=forked_session_id)
        target_writer.append_session_created(**metadata)
        root_branch_id = target_writer.branch_context.branch_id
        id_map = _build_id_map(projected_events)
        for event in projected_events:
            if event.kind == "session.created" or not _can_copy_event(event, source_session_id=source_session_id, id_map=id_map):
                continue
            data = _rewrite_fork_data(
                event.data,
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                root_branch_id=root_branch_id,
                id_map=id_map,
                event_kind=event.kind,
            )
            self.store.append_journal_event(
                session_id=forked_session_id,
                kind=event.kind,
                data=data,
                trace_id=_remap_id(
                    event.trace_id,
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                ),
                observation_id=_remap_id(
                    event.observation_id,
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                ),
                parent_observation_id=_remap_id(
                    event.parent_observation_id,
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                ),
                branch_id=root_branch_id,
            )
        final_metadata = dict(target.metadata)
        final_metadata.pop("session_id", None)
        final_metadata.pop("root_branch_id", None)
        final_metadata["title"] = title or f"Fork of {record.title}"
        target_writer.append_session_metadata_updated(**final_metadata)
        self._copy_archives(source_session_id, forked_session_id, paths=paths)

        session = bootstrap.resume(forked_session_id)
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=catalog.get_session(forked_session_id))

    def _copy_archives(self, source_session_id: str, forked_session_id: str, *, paths: LansCoderPaths) -> None:
        source = paths.archives / source_session_id
        if not source.exists():
            return
        destination = paths.archives / forked_session_id
        shutil.copytree(source, destination, dirs_exist_ok=True)


def _build_id_map(events: list[Any]) -> dict[str, str]:
    from lanscoder.context.identity import new_checkpoint_id, new_message_id, new_part_id
    from lanscoder.journal import new_observation_id, new_trace_id

    id_map: dict[str, str] = {}
    for event in events:
        data = event.data
        _map_event_identity(event.trace_id, id_map, new_trace_id)
        _map_event_identity(event.observation_id, id_map, new_observation_id)
        _map_event_identity(event.parent_observation_id, id_map, new_observation_id)
        if event.kind == "message.appended":
            message_id = data.get("message_id")
            if isinstance(message_id, str) and message_id:
                id_map.setdefault(message_id, new_message_id())
            _collect_part_ids(data.get("parts"), id_map, new_part_id)
            _collect_tool_call_ids_from_parts(data.get("parts"), id_map)
        if event.kind == "checkpoint.created":
            checkpoint_id = data.get("id")
            if isinstance(checkpoint_id, str) and checkpoint_id:
                id_map.setdefault(checkpoint_id, new_checkpoint_id())
        if event.kind in {"compaction.completed", "llm.compaction.completed"}:
            _collect_compaction_part_ids(data.get("event"), id_map, new_part_id)
            _collect_compaction_tool_call_ids(data.get("event"), id_map)
    id_map.pop("", None)
    return id_map


def _can_copy_event(event: Any, *, source_session_id: str, id_map: dict[str, str]) -> bool:
    if event.kind != "trace.linked":
        return True
    return all(_can_remap_linkage(event.data.get(field), source_session_id=source_session_id, id_map=id_map) for field in _EVENT_LINK_FIELDS["trace.linked"] if field in event.data)


def _can_remap_linkage(value: Any, *, source_session_id: str, id_map: dict[str, str]) -> bool:
    return not isinstance(value, str) or not value or value == source_session_id or value in id_map


def _map_event_identity(value: Any, id_map: dict[str, str], factory) -> None:
    if isinstance(value, str) and value:
        id_map.setdefault(value, factory())


def _collect_compaction_part_ids(value: Any, id_map: dict[str, str], factory) -> None:
    if not isinstance(value, dict):
        return
    replacements = value.get("replacements")
    if not isinstance(replacements, list):
        return
    for replacement in replacements:
        if not isinstance(replacement, dict):
            continue
        source_part_id = replacement.get("source_part_id")
        if isinstance(source_part_id, str) and source_part_id:
            id_map.setdefault(source_part_id, factory())
        replacement_part = replacement.get("replacement_part")
        if isinstance(replacement_part, dict):
            _collect_part_ids([replacement_part], id_map, factory)


def _collect_tool_call_ids_from_parts(parts: Any, id_map: dict[str, str]) -> None:
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") not in {"tool_call", "tool_result"}:
            continue
        metadata = part.get("metadata")
        if not isinstance(metadata, dict):
            continue
        tool_call_id = metadata.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            id_map.setdefault(tool_call_id, f"call_{uuid.uuid4().hex[:12]}")


def _collect_compaction_tool_call_ids(value: Any, id_map: dict[str, str]) -> None:
    if not isinstance(value, dict):
        return
    replacements = value.get("replacements")
    if not isinstance(replacements, list):
        return
    for replacement in replacements:
        if not isinstance(replacement, dict):
            continue
        replacement_part = replacement.get("replacement_part")
        if isinstance(replacement_part, dict):
            _collect_tool_call_ids_from_parts([replacement_part], id_map)


def _collect_part_ids(parts: Any, id_map: dict[str, str], factory) -> None:
    if not isinstance(parts, list):
        return
    for part in parts:
        if isinstance(part, dict):
            part_id = part.get("id")
            if isinstance(part_id, str) and part_id:
                id_map.setdefault(part_id, factory())


def _rewrite_fork_data(
    value: Any,
    *,
    source_session_id: str,
    forked_session_id: str,
    root_branch_id: str,
    id_map: dict[str, str],
    key: str | None = None,
    event_kind: str | None = None,
    top_level: bool = True,
) -> Any:
    del key, top_level
    if not isinstance(value, dict):
        return value

    rewritten = _rewrite_link_fields(
        value,
        source_session_id=source_session_id,
        forked_session_id=forked_session_id,
        root_branch_id=root_branch_id,
        id_map=id_map,
        fields=_EVENT_LINK_FIELDS.get(event_kind or "", set()),
    )
    if event_kind == "checkpoint.created":
        rewritten["id"] = _remap_id(
            value.get("id"),
            source_session_id=source_session_id,
            forked_session_id=forked_session_id,
            id_map=id_map,
        )
        for field in ("tail_start_message_id", "covered_until_message_id"):
            if field in value:
                rewritten[field] = _remap_id(
                    value[field],
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                )
    if event_kind == "message.appended":
        if "parts" in value:
            rewritten["parts"] = _rewrite_message_parts(
                value["parts"],
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                id_map=id_map,
            )
    if event_kind == "trace.paused" and isinstance(value.get("pending"), dict):
        pending = dict(value["pending"])
        for field in ("trace_id", "tool_call_id"):
            if field not in pending:
                continue
            pending[field] = _remap_id(
                pending[field],
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                id_map=id_map,
            )
        rewritten["pending"] = pending
    if event_kind in {"compaction.completed", "llm.compaction.completed"}:
        rewritten["event"] = _rewrite_compaction_event(
            value.get("event"),
            source_session_id=source_session_id,
            forked_session_id=forked_session_id,
            root_branch_id=root_branch_id,
            id_map=id_map,
        )
    if event_kind == "provider.projection.consumed" and isinstance(value.get("part_ids"), list):
        rewritten["part_ids"] = [
            _remap_id(
                part_id,
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                id_map=id_map,
            )
            for part_id in value["part_ids"]
        ]
    if isinstance(value.get("dispatch_branch_context"), dict):
        rewritten["dispatch_branch_context"] = _rewrite_link_fields(
            value["dispatch_branch_context"],
            source_session_id=source_session_id,
            forked_session_id=forked_session_id,
            root_branch_id=root_branch_id,
            id_map=id_map,
            fields={"branch_id", "trace_id", "parent_trace_id", "parent_observation_id"},
        )
    return rewritten


def _rewrite_message_parts(
    value: Any,
    *,
    source_session_id: str,
    forked_session_id: str,
    id_map: dict[str, str],
) -> Any:
    if not isinstance(value, list):
        return value
    rewritten_parts: list[Any] = []
    for part in value:
        if not isinstance(part, dict):
            rewritten_parts.append(part)
            continue
        rewritten = dict(part)
        for field in ("id", "message_id"):
            if field in part:
                rewritten[field] = _remap_id(
                    part[field],
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                )
        if part.get("kind") in {"tool_call", "tool_result"} and isinstance(part.get("metadata"), dict):
            metadata = dict(part["metadata"])
            if "tool_call_id" in metadata:
                metadata["tool_call_id"] = _remap_id(
                    metadata["tool_call_id"],
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                )
            rewritten["metadata"] = metadata
        rewritten_parts.append(rewritten)
    return rewritten_parts


def _rewrite_compaction_event(
    value: Any,
    *,
    source_session_id: str,
    forked_session_id: str,
    root_branch_id: str,
    id_map: dict[str, str],
) -> Any:
    if not isinstance(value, dict):
        return value
    rewritten = _rewrite_link_fields(
        value,
        source_session_id=source_session_id,
        forked_session_id=forked_session_id,
        root_branch_id=root_branch_id,
        id_map=id_map,
        fields={"checkpoint_id"},
    )
    for field in ("source_part_ids", "output_part_ids"):
        if isinstance(value.get(field), list):
            rewritten[field] = [
                _remap_id(
                    item,
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                )
                for item in value[field]
            ]
    replacements = value.get("replacements")
    if not isinstance(replacements, list):
        return rewritten
    rewritten_replacements: list[Any] = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            rewritten_replacements.append(replacement)
            continue
        rewritten_replacement = dict(replacement)
        for field in ("message_id", "source_part_id"):
            if field in replacement:
                rewritten_replacement[field] = _remap_id(
                    replacement[field],
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    id_map=id_map,
                )
        if "replacement_part" in replacement:
            rewritten_replacement["replacement_part"] = _rewrite_message_parts(
                [replacement["replacement_part"]],
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                id_map=id_map,
            )[0]
        rewritten_replacements.append(rewritten_replacement)
    rewritten["replacements"] = rewritten_replacements
    return rewritten


def _rewrite_link_fields(
    value: dict[str, Any],
    *,
    source_session_id: str,
    forked_session_id: str,
    root_branch_id: str,
    id_map: dict[str, str],
    fields: set[str],
) -> dict[str, Any]:
    rewritten = dict(value)
    for field in fields:
        if field not in value:
            continue
        if field in {"branch_id", "root_branch_id", "parent_branch_id", "new_branch_id"}:
            rewritten[field] = root_branch_id
            continue
        rewritten[field] = _remap_id(
            value[field],
            source_session_id=source_session_id,
            forked_session_id=forked_session_id,
            id_map=id_map,
        )
    return rewritten


def _remap_id(
    value: Any,
    *,
    source_session_id: str,
    forked_session_id: str,
    id_map: dict[str, str],
) -> Any:
    if value == source_session_id:
        return forked_session_id
    return id_map.get(value, value) if isinstance(value, str) else value
