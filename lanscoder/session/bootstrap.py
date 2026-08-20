from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lanscoder.agent.prompt_inputs import read_agents_md
from lanscoder.agent.session import AgentSession, create_project_permission_manager
from lanscoder.context.identity import new_session_id
from lanscoder.context.store import JsonlSessionStore
from lanscoder.memory.manager import MemoryManager, project_memory_root
from lanscoder.permissions.grants import FilePermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.skills.discovery import discover_all_skills
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class SessionBootstrap:

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    user_memory_root: str | Path | None = None

    def resolved_data_root(self) -> Path:
        return Path(self.data_root) if self.data_root is not None else self.store.root

    def resolve_tools(self) -> list[Tool] | None:
        return self.tools_provider() if self.tools_provider is not None else self.tools

    def permission_manager(self) -> PermissionManager:
        return create_project_permission_manager(
            self.project_root,
            grants=FilePermissionGrantStore(self.resolved_data_root() / "permissions.json"),
        )

    def memory_manager(self) -> MemoryManager:
        user_root = Path(self.user_memory_root) if self.user_memory_root is not None else Path.home() / ".lanscoder" / "memory"
        return MemoryManager(
            user_root=user_root,
            project_root=project_memory_root(self.resolved_data_root(), Path(self.project_root)),
        )

    def create(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession.create(
            store=self.store,
            session_id=session_id or new_session_id(),
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )

    def resume(self, session_id: str) -> AgentSession:
        return AgentSession.resume(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )

    def from_project(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession.from_project(
            store=self.store,
            session_id=session_id or new_session_id(),
            project_root=self.project_root,
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )
