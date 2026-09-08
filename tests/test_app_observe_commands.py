from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lanscoder.app.observe_commands import ObserveCommandHandler, ObservatoryServerManager
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.core.runtime import CurrentSessionState
from lanscoder.agent.session import AgentSession
from lanscoder.storage import LansCoderPaths


@dataclass
class FakeServer:
    url: str = "http://127.0.0.1:43123/"
    start_calls: int = 0
    shutdown_calls: int = 0

    def start(self) -> "FakeServer":
        self.start_calls += 1
        return self

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _handler(tmp_path: Path, *, active_trace_id: str | None = None, recent_trace_id: str | None = None):
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_observe")
    writer.append_session_created(title="Observe", project_id=paths.project_id, kind="primary")
    if recent_trace_id is not None:
        writer.store.journal.append(
            kind="trace.started",
            data={},
            session_id="sess_observe",
            trace_id=recent_trace_id,
            branch_id=writer.branch_context.branch_id,
        )
    session = AgentSession.resume(store=store, session_id="sess_observe", agents_md="")
    current = CurrentSessionState(session)
    server = FakeServer()
    manager = ObservatoryServerManager(paths, server_factory=lambda _: server)
    handler = ObserveCommandHandler(
        current_session=current,
        active_trace_id=lambda: active_trace_id,
        server_manager=manager,
    )
    return handler, manager, server


def test_observe_routes_the_active_trace_before_other_targets(tmp_path: Path) -> None:
    handler, _, server = _handler(tmp_path, active_trace_id="trc_active", recent_trace_id="trc_recent")

    result = handler.handle("/observe")

    assert result.action == {"type": "open_observatory", "url": f"{server.url}traces/trc_active"}
    assert "If your browser did not open" in result.output


def test_observe_routes_the_most_recent_trace_when_no_trace_is_active(tmp_path: Path) -> None:
    handler, _, server = _handler(tmp_path, recent_trace_id="trc_recent")

    result = handler.handle("/observe")

    assert result.action == {"type": "open_observatory", "url": f"{server.url}traces/trc_recent"}


def test_observe_routes_an_empty_session_to_its_trace_filter(tmp_path: Path) -> None:
    handler, _, server = _handler(tmp_path)

    result = handler.handle("/observe")

    assert result.action == {"type": "open_observatory", "url": f"{server.url}traces?session_id=sess_observe"}


def test_observe_manager_starts_once_and_closes_its_server(tmp_path: Path) -> None:
    handler, manager, server = _handler(tmp_path)

    handler.handle("/observe")
    handler.handle("/observe")
    manager.shutdown()

    assert server.start_calls == 1
    assert server.shutdown_calls == 1
