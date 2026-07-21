"""session resume 编排入口。

resume 的底层事实仍来自完整 append-only event log；checkpoint 只影响下一轮
provider context 投影，不是 resume 存储边界。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.versions import CONTEXT_EVENT_SCHEMA_VERSION
from firstcoder.session.bootstrap import SessionBootstrap
from firstcoder.session.catalog import SessionCatalog, require_usable_record
from firstcoder.session.errors import SessionUnsupportedSchemaError
from firstcoder.session.models import ResumeResult
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class ResumeService:
    """把用户可见 resume 入口封装成窄服务。"""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    catalog: SessionCatalog | None = None

    def resume(self, session_id: str) -> ResumeResult:
        catalog = self.catalog or SessionCatalog(self.store.root)
        record = require_usable_record(catalog.get_session(session_id))
        validate_session_schema(self.store, session_id)

        bootstrap = SessionBootstrap(
            store=self.store,
            project_root=self.project_root,
            data_root=self.data_root,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        session = bootstrap.resume(session_id)
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=record)


def validate_session_schema(store: JsonlSessionStore, session_id: str) -> None:
    """Reject event logs that cannot be replayed by the current runtime."""

    session_created = next(
        (event for event in store.list_events(session_id) if event.type == "session_created"),
        None,
    )
    actual = (
        session_created.payload.get("context_event_schema_version")
        if session_created is not None
        else None
    )
    actual_version = str(actual) if actual is not None else "missing"
    if actual_version != CONTEXT_EVENT_SCHEMA_VERSION:
        raise SessionUnsupportedSchemaError(
            session_id=session_id,
            actual_version=actual_version,
            expected_version=CONTEXT_EVENT_SCHEMA_VERSION,
        )
