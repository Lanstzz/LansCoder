"""会话级工具注册表工厂。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Collection, Protocol

from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.context.task_boundary import TaskBoundaryPolicy, TaskBoundaryService
from lanscoder.memory.manager import MemoryManager
from lanscoder.permissions.manager import PermissionManager
from lanscoder.planning.service import TaskPlanService
from lanscoder.skills.models import SkillCatalog
from lanscoder.tools.load_skill import create_load_skill_tool
from lanscoder.tools.memory_tools import create_memory_tools
from lanscoder.tools.permission_registry import PermissionAwareToolRegistry
from lanscoder.tools.retrieve_archive import create_retrieve_archive_tool
from lanscoder.tools.registry import ToolRegistry
from lanscoder.tools.task_boundary import create_task_boundary_tool
from lanscoder.tools.task_create import create_task_create_tool
from lanscoder.tools.task_list import create_task_list_tool
from lanscoder.tools.task_revise import create_task_revise_tool
from lanscoder.tools.task_update import create_task_update_tool
from lanscoder.tools.types import Tool


class ToolRegistryLike(Protocol):
    """AgentSession 需要的工具注册表最小接口。"""

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
    single_observation_basis_message_ids: Collection[str] = (),
    task_boundary_required_stable_count: int = 2,
    permission_manager: PermissionManager | None = None,
    archive_root: str | Path | None = None,
    current_turn: Callable[[], int] | None = None,
    store: JsonlSessionStore | None = None,
    writer: SessionEventWriter | None = None,
    skill_catalog: SkillCatalog | None = None,
    memory_manager: MemoryManager | None = None,
) -> ToolRegistryLike:
    """创建单个会话专用的工具注册表。

    `task_boundary` 依赖当前会话的 `SessionRuntimeState`，不能放进无状态默认工具集。
    这个工厂集中处理会话级注入，后续权限、确认策略也可以在这里包一层。
    """

    state = runtime_state or SessionRuntimeState(session_id=session_id)
    boundary_service = TaskBoundaryService(
        required_stable_count=task_boundary_required_stable_count,
        known_message_ids=known_message_ids,
        policy=TaskBoundaryPolicy(single_observation_basis_message_ids=single_observation_basis_message_ids),
    )
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
    if "task_boundary" not in registry.names():
        registry.register(create_task_boundary_tool(state, service=boundary_service))
    if (store is None) != (writer is None):
        raise ValueError("store and writer must be provided together")
    if store is not None and writer is not None:
        if writer.store is not store or writer.session_id != session_id:
            raise ValueError("task-plan service requires the live session store and writer")
        service = TaskPlanService(
            store=store,
            writer=writer,
        )
        for tool in (
            create_task_create_tool(service),
            create_task_update_tool(service),
            create_task_revise_tool(service),
            create_task_list_tool(service),
            create_load_skill_tool(skill_catalog or SkillCatalog(), writer),
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
