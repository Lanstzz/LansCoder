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
            if event.kind == "session.created":
                continue
            data = _rewrite_fork_data(
                event.data,
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                root_branch_id=root_branch_id,
                id_map=id_map,
            )
            self.store.append_journal_event(
                session_id=forked_session_id,
                kind=event.kind,
                data=data,
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

    id_map: dict[str, str] = {}
    for event in events:
        data = event.data
        message_id = data.get("message_id")
        if isinstance(message_id, str) and message_id:
            id_map.setdefault(message_id, new_message_id())
        if event.kind == "checkpoint.created":
            checkpoint_id = data.get("id")
            if isinstance(checkpoint_id, str) and checkpoint_id:
                id_map.setdefault(checkpoint_id, new_checkpoint_id())
        _collect_part_ids(data.get("parts"), id_map, new_part_id)
        event_data = data.get("event")
        if isinstance(event_data, dict):
            for replacement in event_data.get("replacements", []):
                if not isinstance(replacement, dict):
                    continue
                id_map.setdefault(str(replacement.get("source_part_id") or ""), new_part_id())
                replacement_part = replacement.get("replacement_part")
                if isinstance(replacement_part, dict):
                    _collect_part_ids([replacement_part], id_map, new_part_id)
        _collect_tool_call_ids(data, id_map)
    id_map.pop("", None)
    return id_map


def _collect_tool_call_ids(value: Any, id_map: dict[str, str]) -> None:
    if isinstance(value, dict):
        tool_call_id = value.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            id_map.setdefault(tool_call_id, f"call_{uuid.uuid4().hex[:12]}")
        for item in value.values():
            _collect_tool_call_ids(item, id_map)
    elif isinstance(value, list):
        for item in value:
            _collect_tool_call_ids(item, id_map)


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
) -> Any:
    if isinstance(value, dict):
        return {
            field: (
                value_item
                if field == "forked_from"
                else root_branch_id
                if field in {"branch_id", "root_branch_id", "parent_branch_id", "new_branch_id"}
                else _rewrite_fork_data(
                    value_item,
                    source_session_id=source_session_id,
                    forked_session_id=forked_session_id,
                    root_branch_id=root_branch_id,
                    id_map=id_map,
                    key=field,
                )
            )
            for field, value_item in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite_fork_data(
                item,
                source_session_id=source_session_id,
                forked_session_id=forked_session_id,
                root_branch_id=root_branch_id,
                id_map=id_map,
                key=key,
            )
            for item in value
        ]
    if key == "archive_id" or value is None:
        return value
    if value == source_session_id:
        return forked_session_id
    return id_map.get(value, value) if isinstance(value, str) else value
