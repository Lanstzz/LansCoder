from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.versions import CONTEXT_EVENT_SCHEMA_VERSION
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.access import SessionAccessPolicy
from lanscoder.session.catalog import SessionCatalog, require_usable_record
from lanscoder.session.catalog import is_safe_session_id
from lanscoder.session.errors import (
    SessionInvalidIdError,
    SessionCorruptError,
    SessionEmptyError,
    SessionNotFoundError,
    SessionUnsupportedSchemaError,
)
from lanscoder.session.models import ResumeResult
from lanscoder.journal import JournalCorruptError
from lanscoder.storage import LansCoderPaths
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class ResumeService:

    store: JsonlSessionStore
    project_root: str | Path
    paths: LansCoderPaths | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    catalog: SessionCatalog | None = None

    def resume(self, session_id: str) -> ResumeResult:
        validate_session_schema(self.store, session_id)
        catalog = self.catalog or SessionCatalog(self.store.root)
        policy = SessionAccessPolicy(self.project_root, journal=catalog)
        policy.open_primary(session_id)
        bootstrap = SessionBootstrap(
            store=self.store,
            project_root=self.project_root,
            paths=self.paths,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        record = require_usable_record(catalog.get_session(session_id))

        session = bootstrap.resume(session_id)
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=record)


def validate_session_schema(store: JsonlSessionStore, session_id: str) -> None:

    if not is_safe_session_id(session_id):
        raise SessionInvalidIdError(f"invalid session_id: {session_id!r}")
    path = store.sessions_dir / f"{session_id}.jsonl"
    if not path.exists():
        raise SessionNotFoundError(f"session not found: {session_id}")

    try:
        events = store.journal.read_events(session_id)
    except JournalCorruptError as error:
        raise SessionCorruptError(str(error)) from error
    if not events:
        raise SessionEmptyError(f"session {session_id} is empty")
    session_created = next((event for event in events if event.kind == "session.created"), None)
    if session_created is None:
        raise SessionCorruptError(f"session {session_id} has no valid session_created event")
    actual = session_created.data.get("context_event_schema_version")
    actual_version = str(actual) if actual is not None else "missing"
    if actual_version != CONTEXT_EVENT_SCHEMA_VERSION:
        raise SessionUnsupportedSchemaError(
            session_id=session_id,
            actual_version=actual_version,
            expected_version=CONTEXT_EVENT_SCHEMA_VERSION,
        )
