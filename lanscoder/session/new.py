from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.access import SessionAccessPolicy
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.models import ResumeResult, SessionRecord
from lanscoder.storage import LansCoderPaths
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


class _ProvisionalSessionBootstrap(SessionBootstrap):
    def access_policy(self) -> SessionAccessPolicy:
        return SessionAccessPolicy(self.paths.project_root, project_id=self.paths.project_id)


@dataclass(slots=True)
class NewSessionService:
    store: JsonlSessionStore
    project_root: str | Path
    paths: LansCoderPaths | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None

    def create(self, *, title: str | None = None, provisional: bool = False) -> ResumeResult:
        bootstrap_type = _ProvisionalSessionBootstrap if provisional else SessionBootstrap
        bootstrap = bootstrap_type(
            store=self.store,
            project_root=self.project_root,
            paths=self.paths,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        session = bootstrap.create_provisional_primary() if provisional else bootstrap.create()
        if title:
            if provisional:
                session.activation_metadata["title"] = title
            else:
                SessionEventWriter(store=self.store, session_id=session.session_id).append_session_metadata_updated(title=title)
        if provisional:
            record = SessionRecord(
                session_id=session.session_id,
                title=title or session.session_id,
                metadata=dict(session.activation_metadata),
            )
        else:
            record = SessionCatalog(self.store.root).get_session(session.session_id)
        return ResumeResult(session=session, record=record)
