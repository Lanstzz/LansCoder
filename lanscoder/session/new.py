"""Create fresh interactive sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.models import ResumeResult
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class NewSessionService:
    """Create a new session and return its runtime object."""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None

    def create(self, *, title: str | None = None) -> ResumeResult:
        bootstrap = SessionBootstrap(
            store=self.store,
            project_root=self.project_root,
            data_root=self.data_root,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        session = bootstrap.create()
        if title:
            SessionEventWriter(store=self.store, session_id=session.session_id).append_session_metadata_updated(title=title)
        record = SessionCatalog(self.store.root).get_session(session.session_id)
        return ResumeResult(session=session, record=record)
