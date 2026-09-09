"""TUI Observatory command and embedded server lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urlencode

from lanscoder.app.commands import CommandResult
from lanscoder.observability.web import ObservatoryServer
from lanscoder.storage import LansCoderPaths


class CurrentSessionLike(Protocol):
    session_id: str
    session: object


class ObservatoryServerLike(Protocol):
    url: str

    def start(self) -> "ObservatoryServerLike": ...

    def shutdown(self) -> None: ...


@dataclass(slots=True)
class ObservatoryServerManager:
    """Start one embedded Observatory server lazily and own its shutdown."""

    paths: LansCoderPaths
    server_factory: Callable[[LansCoderPaths], ObservatoryServerLike] = ObservatoryServer
    _server: ObservatoryServerLike | None = field(default=None, init=False)

    def start(self) -> ObservatoryServerLike:
        if self._server is None:
            self._server = self.server_factory(self.paths).start()
        return self._server

    def shutdown(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server = None


@dataclass(slots=True)
class ObserveCommandHandler:
    """Route /observe to the active trace, latest session trace, or session filter."""

    current_session: CurrentSessionLike
    active_trace_id: Callable[[], str | None]
    server_manager: ObservatoryServerManager

    def commands(self) -> list[tuple[str, str]]:
        return [("/observe", "Open the local Observatory for the current session.")]

    def handle(self, text: str) -> CommandResult:
        if " ".join(text.strip().split()) != "/observe":
            return CommandResult(handled=False)
        server = self.server_manager.start()
        url = self._deep_link(server.url)
        return CommandResult(
            handled=True,
            output=f"Observatory opened: {url}\nIf your browser did not open, visit: {url}",
            action={"type": "open_observatory", "url": url},
        )

    def _deep_link(self, server_url: str) -> str:
        if not self._has_persisted_root():
            return f"{server_url}traces"
        trace_id = self.active_trace_id() or self._latest_session_trace_id()
        if trace_id:
            return f"{server_url}traces/{quote(trace_id, safe='')}"
        session_id = self.current_session.session_id
        return f"{server_url}traces?{urlencode({'session_id': session_id})}"

    def _has_persisted_root(self) -> bool:
        session = self.current_session.session
        return getattr(getattr(session, "writer", None), "branch_context", None) is not None

    def _latest_session_trace_id(self) -> str | None:
        session = self.current_session.session
        store = getattr(session, "store", None)
        journal = getattr(store, "journal", None)
        read_events = getattr(journal, "read_events", None)
        if read_events is None:
            return None
        try:
            events = read_events(self.current_session.session_id)
        except Exception:
            return None
        for event in reversed(events):
            if getattr(event, "kind", None) != "trace.started":
                continue
            trace_id = getattr(event, "trace_id", None)
            if isinstance(trace_id, str) and trace_id:
                return trace_id
        return None
