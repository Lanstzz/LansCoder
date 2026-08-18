"""持久记忆工具：remember / forget / read_memory / search_memory。"""

from __future__ import annotations

from typing import Any

from lanscoder.context.writer import SessionEventWriter
from lanscoder.memory.manager import MemoryManager
from lanscoder.memory.models import MEMORY_TYPES, MemoryRecord, MemoryScope, validate_record
from lanscoder.providers.types import ToolDefinition
from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from lanscoder.utils.schema import object_schema


def create_memory_tools(memory_manager: MemoryManager, writer: SessionEventWriter | None = None) -> list[Tool]:
    return [
        _remember_tool(memory_manager, writer),
        _forget_tool(memory_manager, writer),
        _read_memory_tool(memory_manager),
        _search_memory_tool(memory_manager),
    ]


def _resolve_scope(value: str | None) -> MemoryScope:
    return MemoryScope.PROJECT if value in (None, "") else MemoryScope(value)


def _search_scopes(scope: str) -> list[MemoryScope]:
    if scope == "all":
        return [MemoryScope.USER, MemoryScope.PROJECT]
    return [_resolve_scope(scope)]


def _append_memory_event(writer: SessionEventWriter | None, scope: MemoryScope, name: str, action: str) -> None:
    if writer is None:
        return
    writer.append_event("memory_updated", {"scope": scope.value, "name": name, "action": action})


def _remember_tool(manager: MemoryManager, writer: SessionEventWriter | None) -> Tool:
    def remember(*, name: str, description: str, body: str, type: str = "project", scope: str = "project") -> ToolResult:
        try:
            resolved_scope = _resolve_scope(scope)
            record = MemoryRecord(name=name, description=description, type=type, body=body)
            validate_record(record)
            manager.write(resolved_scope, record)
        except ValueError as exc:
            return make_error_result("remember", f"Unable to remember: {exc}")
        _append_memory_event(writer, resolved_scope, name, "upsert")
        return make_text_result(
            "remember",
            f"Saved {resolved_scope.value} memory '{name}'.",
            scope=resolved_scope.value,
            type=type,
        )

    parameters = _scoped_schema(
        {
            "name": {"type": "string", "description": "kebab-case slug for the memory, e.g. build-commands."},
            "description": {"type": "string", "description": "One-line summary used to decide relevance during recall."},
            "body": {"type": "string", "description": "The durable fact to remember."},
            "type": {"type": "string", "enum": sorted(MEMORY_TYPES), "default": "project"},
            "scope": {"type": "string", "enum": ["project", "user"], "default": "project"},
        },
        required=["name", "description", "body"],
    )
    return Tool(
        definition=ToolDefinition(
            name="remember",
            description=(
                "Save a durable fact to persistent memory. One memory per name; writing the "
                "same name overwrites. Use project scope for repo-specific facts and user scope "
                "for cross-project preferences."
            ),
            parameters=parameters,
        ),
        executor=remember,
    )


def _forget_tool(manager: MemoryManager, writer: SessionEventWriter | None) -> Tool:
    def forget(*, name: str, scope: str = "project") -> ToolResult:
        try:
            resolved_scope = _resolve_scope(scope)
        except ValueError as exc:
            return make_error_result("forget", f"Unable to forget: {exc}")
        deleted = manager.delete(resolved_scope, name)
        if not deleted:
            return make_text_result(
                "forget",
                f"{resolved_scope.value} memory named '{name}' not found.",
                scope=resolved_scope.value,
                found=False,
            )
        _append_memory_event(writer, resolved_scope, name, "delete")
        return make_text_result(
            "forget",
            f"Forgot {resolved_scope.value} memory '{name}'.",
            scope=resolved_scope.value,
            found=True,
        )

    parameters = _scoped_schema(
        {
            "name": {"type": "string"},
            "scope": {"type": "string", "enum": ["project", "user"], "default": "project"},
        },
        required=["name"],
    )
    return Tool(
        definition=ToolDefinition(
            name="forget",
            description="Delete a named persistent memory.",
            parameters=parameters,
        ),
        executor=forget,
    )


def _read_memory_tool(manager: MemoryManager) -> Tool:
    def read_memory(*, name: str, scope: str = "project") -> ToolResult:
        try:
            resolved_scope = _resolve_scope(scope)
        except ValueError as exc:
            return make_error_result("read_memory", f"Unable to read memory: {exc}")
        record = manager.get(resolved_scope, name)
        if record is None:
            return make_text_result(
                "read_memory",
                f"{resolved_scope.value} memory named '{name}' not found.",
                scope=resolved_scope.value,
                found=False,
            )
        return make_text_result(
            "read_memory",
            _render_record(record),
            scope=resolved_scope.value,
            type=record.type,
            found=True,
        )

    parameters = _scoped_schema(
        {
            "name": {"type": "string"},
            "scope": {"type": "string", "enum": ["project", "user"], "default": "project"},
        },
        required=["name"],
    )
    return Tool(
        definition=ToolDefinition(
            name="read_memory",
            description="Read the full content of one persistent memory by exact name.",
            parameters=parameters,
        ),
        executor=read_memory,
    )


def _search_memory_tool(manager: MemoryManager) -> Tool:
    def search_memory(*, query: str, scope: str = "project") -> ToolResult:
        try:
            scopes = _search_scopes(scope)
        except ValueError as exc:
            return make_error_result("search_memory", f"Unable to search memory: {exc}")
        if not query.strip():
            return make_text_result("search_memory", f"No memories match '{query}'.", query=query, count=0)
        needle = query.lower()
        matches: list[tuple[MemoryScope, MemoryRecord]] = []
        for candidate_scope in scopes:
            for record in manager.list(candidate_scope):
                haystack = "\n".join([record.name, record.description, record.body]).lower()
                if needle in haystack:
                    matches.append((candidate_scope, record))
        if not matches:
            return make_text_result("search_memory", f"No memories match '{query}'.", query=query, count=0)
        lines = [f"Matched {len(matches)} memories for '{query}':", ""]
        for record_scope, record in matches:
            first_line = next((line for line in record.body.splitlines() if line.strip()), "")
            lines.append(f"- [{record.name}] ({record_scope.value}) {record.description}")
            if first_line:
                lines.append(f"    {first_line}")
        return make_text_result("search_memory", "\n".join(lines), query=query, count=len(matches))

    parameters = _scoped_schema(
        {
            "query": {"type": "string", "description": "Substring to match across names, descriptions, and bodies."},
            "scope": {"type": "string", "enum": ["project", "user", "all"], "default": "project"},
        },
        required=["query"],
    )
    return Tool(
        definition=ToolDefinition(
            name="search_memory",
            description="Search persistent memory by substring across names, descriptions, and bodies.",
            parameters=parameters,
        ),
        executor=search_memory,
    )


def _render_record(record: MemoryRecord) -> str:
    return "\n".join(
        [
            f"name: {record.name}",
            f"description: {record.description}",
            f"type: {record.type}",
            "---",
            record.body,
        ]
    )


def _scoped_schema(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    schema = object_schema(properties, required=required)
    schema["additionalProperties"] = False
    return schema
