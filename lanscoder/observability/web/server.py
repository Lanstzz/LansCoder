"""Threading HTTP server lifecycle and browser launcher."""

from __future__ import annotations

import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from lanscoder.storage import LansCoderPaths

from .api import ObservatoryQueryService
from .handler import ObservatoryRequestHandler


class _BoundHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class ObservatoryServer:
    """A loopback-only server that can be embedded or run in the foreground."""

    def __init__(self, paths: LansCoderPaths | str | Path) -> None:
        self.paths = paths if isinstance(paths, LansCoderPaths) else LansCoderPaths(storage_root=paths)
        self.query_service = ObservatoryQueryService(self.paths)
        self._httpd: _BoundHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            raise RuntimeError("ObservatoryServer has not started")
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}/"

    @property
    def httpd(self) -> _BoundHTTPServer:
        if self._httpd is None:
            raise RuntimeError("ObservatoryServer has not started")
        return self._httpd

    @property
    def server_address(self) -> tuple[str, int]:
        return self.address

    def start(self) -> "ObservatoryServer":
        if self._httpd is not None:
            return self
        self._bind_httpd()
        assert self._httpd is not None
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="lanscoder-observatory", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        if self._httpd is None:
            self._bind_httpd()
        assert self._httpd is not None
        if self._thread is not None:
            self._thread.join()
            return
        self._httpd.serve_forever(poll_interval=poll_interval)

    def _bind_httpd(self) -> None:
        self._httpd = _BoundHTTPServer(("127.0.0.1", 0), ObservatoryRequestHandler)
        self._httpd.query_service = self.query_service  # type: ignore[attr-defined]

    def shutdown(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None

    close = shutdown


def launch_browser(url: str) -> str:
    """Attempt to open *url* and always return it for manual fallback."""

    try:
        webbrowser.open(url)
    except Exception:
        pass
    return url


def open_observatory(server: ObservatoryServer) -> str:
    """Open a running server and return its URL even when a browser is absent."""

    return launch_browser(server.url)
