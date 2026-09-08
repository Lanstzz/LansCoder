from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lanscoder.agent.prompt_inputs import read_agents_md
from lanscoder.agent.session import AgentSession, create_project_permission_manager
from lanscoder.context.store import JsonlSessionStore
from lanscoder.memory.manager import MemoryManager
from lanscoder.observability.protocol import NoOpTraceRecorder, TraceRecorder
from lanscoder.observability.recorder import JournalTraceRecorder
from lanscoder.permissions.grants import FilePermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.session.access import SessionAccessPolicy
from lanscoder.session.errors import SessionNotFoundError
from lanscoder.skills.discovery import discover_all_skills
from lanscoder.storage import LansCoderPaths, PayloadStore
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


class _SessionCatalogLookup:
    def __init__(self, catalog: object) -> None:
        self.catalog = catalog

    def lookup(self, session_id: str) -> object | None:
        try:
            return self.catalog.get_session(session_id)
        except SessionNotFoundError:
            return None


@dataclass(slots=True)
class SessionBootstrap:
    store: JsonlSessionStore
    project_root: str | Path
    paths: LansCoderPaths | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    user_memory_root: str | Path | None = None

    def __post_init__(self) -> None:
        if self.paths is None:
            self.paths = LansCoderPaths(storage_root=self.store.root, project_root=self.project_root)
        elif self.paths.project_root != Path(self.project_root).expanduser().resolve(strict=False):
            raise ValueError("paths.project_root must match project_root")
        if self.store.root.expanduser().resolve(strict=False) != self.paths.storage_root.resolve(strict=False):
            raise ValueError("store.root must match paths.storage_root")

    def access_policy(self) -> SessionAccessPolicy:
        from lanscoder.session.catalog import SessionCatalog

        catalog = SessionCatalog(self.paths.storage_root)
        return SessionAccessPolicy(
            self.paths.project_root,
            journal=_SessionCatalogLookup(catalog),
        )

    def resolve_tools(self) -> list[Tool] | None:
        return self.tools_provider() if self.tools_provider is not None else self.tools

    def create_trace_recorder(self) -> TraceRecorder:
        try:
            return JournalTraceRecorder(self.store.journal, PayloadStore(self.paths))
        except Exception:
            return NoOpTraceRecorder()

    def permission_manager(self) -> PermissionManager:
        return create_project_permission_manager(
            self.paths.project_root,
            grants=FilePermissionGrantStore(self.paths.permissions),
        )

    def memory_manager(self) -> MemoryManager:
        user_root = Path(self.user_memory_root) if self.user_memory_root is not None else self.paths.memory
        return MemoryManager(
            user_root=user_root,
            project_root=self.paths.project_memory,
        )

    def create(self, *, session_id: str | None = None) -> AgentSession:
        descriptor = self.access_policy().create_primary(session_id)
        session = AgentSession.create(
            store=self.store,
            session_id=descriptor.session_id,
            agents_md=read_agents_md(self.paths.project_root),
            skill_catalog=discover_all_skills(self.paths.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
            session_metadata=descriptor.metadata,
        )
        return session

    def resume(self, session_id: str) -> AgentSession:
        self.access_policy().open_primary(session_id)
        return AgentSession.resume(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.paths.project_root),
            skill_catalog=discover_all_skills(self.paths.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )

    def from_project(self, *, session_id: str | None = None) -> AgentSession:
        return self.create(session_id=session_id)
