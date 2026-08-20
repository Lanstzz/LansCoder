from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Collection, Protocol

from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.memory.manager import MemoryManager
from lanscoder.permissions.manager import PermissionManager
from lanscoder.planning.service import TaskPlanService
from lanscoder.skills.models import SkillCatalog
from lanscoder.tools.load_skill import create_load_skill_tool
from lanscoder.tools.memory_tools import create_memory_tools
from lanscoder.tools.permission_registry import PermissionAwareToolRegistry
from lanscoder.tools.retrieve_archive import create_retrieve_archive_tool
from lanscoder.tools.registry import ToolRegistry
from lanscoder.tools.task_create import create_task_create_tool
from lanscoder.tools.task_list import create_task_list_tool
from lanscoder.tools.task_revise import create_task_revise_tool
from lanscoder.tools.task_update import create_task_update_tool
from lanscoder.tools.types import Tool


class ToolRegistryLike(Protocol):

    def register(self, tool: Tool) -> None: ...

    def definitions(self): ...

    def names(self) -> list[str]: ...

    def tools(self) -> list[Tool]: ...

    def execute(self, name: str, arguments=None): ...


def create_session_tool_registry(
    *,
    session_id: str,
    runtime_state: SessionRuntimeState | None = None,
    tools: list[Tool] | None = None,
    known_message_ids: Collection[str] | None = None,
    permission_manager: PermissionManager | None = None,
    archive_root: str | Path | None = None,
    current_turn: Callable[[], int] | None = None,
    store: JsonlSessionStore | None = None,
    writer: SessionEventWriter | None = None,
    skill_catalog: SkillCatalog | None = None,
    get_skill_catalog: Callable[[], SkillCatalog] | None = None,
    memory_manager: MemoryManager | None = None,
) -> ToolRegistryLike:

    supplied_tools = tools or []
    reserved_names = {
        "retrieve_archive",
        "task_create",
        "task_update",
        "task_revise",
        "task_list",
        "load_skill",
    }
    conflicting = next((tool.name for tool in supplied_tools if tool.name in reserved_names), None)
    if conflicting is not None:
        raise ValueError(f"{conflicting} is a reserved session-scoped tool name")
    registry = ToolRegistry(supplied_tools)
    if (store is None) != (writer is None):
        raise ValueError("store and writer must be provided together")
    if store is not None and writer is not None:
        if writer.store is not store or writer.session_id != session_id:
            raise ValueError("task-plan service requires the live session store and writer")
        service = TaskPlanService(
            store=store,
            writer=writer,
        )
        get_catalog = get_skill_catalog or (lambda: skill_catalog or SkillCatalog())
        for tool in (
            create_task_create_tool(service),
            create_task_update_tool(service),
            create_task_revise_tool(service),
            create_task_list_tool(service),
            create_load_skill_tool(get_catalog, writer),
        ):
            registry.register(tool)
    if memory_manager is not None:
        for tool in create_memory_tools(memory_manager, writer):
            registry.register(tool)
    if archive_root is not None:
        registry.register(
            create_retrieve_archive_tool(
                session_id=session_id,
                archive_root=archive_root,
                current_turn=current_turn or (lambda: 0),
            )
        )
    if permission_manager is not None:
        return PermissionAwareToolRegistry(registry, permission_manager)
    return registry
