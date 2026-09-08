"""HTTP routing for the read-only local Observatory."""

from __future__ import annotations

import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from .api import ObservatoryProblem, ObservatoryQueryService, json_bytes, parse_query


class ObservatoryRequestHandler(BaseHTTPRequestHandler):
    """Serve only GET resources backed by an injected query service."""

    server_version = "LansCoderObservatory/1"

    @property
    def query_service(self) -> ObservatoryQueryService:
        return self.server.query_service  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/healthz":
                self._json({"status": "ok"})
            elif path == "/api/v1/traces":
                self._json(self.query_service.list_traces(parse_query(parsed.query)))
            elif path.startswith("/api/v1/traces/") and _is_exact(path, "/api/v1/traces/", 4):
                self._json(self.query_service.get_trace(_component(path, 3)))
            elif path == "/api/v1/sessions":
                self._json(self.query_service.list_sessions())
            elif path.startswith("/api/v1/sessions/") and path.endswith("/replay") and _is_exact(path, "/api/v1/sessions/", 5):
                branch = _single_query_value(parse_query(parsed.query), "branch")
                self._json(self.query_service.replay_session(_component(path, 3), branch))
            elif path.startswith("/api/v1/payloads/") and _is_exact(path, "/api/v1/payloads/", 4):
                size_bytes = _single_query_value(parse_query(parsed.query), "size_bytes")
                data, media_type = self.query_service.read_payload(_component(path, 3), size_bytes)
                self._bytes(data, media_type)
            elif path == "/" or path == "/traces" or path == "/sessions":
                self._static("index.html")
            elif path.startswith("/traces/") and _is_exact(path, "/traces/", 2):
                self._static("index.html")
            elif path.startswith("/sessions/") and _is_exact(path, "/sessions/", 2):
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path.removeprefix("/static/"))
            else:
                self._problem("not_found", "resource not found", HTTPStatus.NOT_FOUND)
        except ObservatoryProblem as error:
            self._json(error.to_dict(), status=error.status)
        except (ValueError, KeyError):
            self._problem("not_found", "resource not found", HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_TRACE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def __getattr__(self, name: str) -> Any:
        # BaseHTTPRequestHandler otherwise emits 501 for unknown verbs. The
        # Observatory boundary is uniformly read-only, so every verb gets the
        # same explicit 405 response and Allow header.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, value: Any, *, status: int = HTTPStatus.OK) -> None:
        self._bytes(json_bytes(value), "application/json; charset=utf-8", status=status)

    def _problem(self, code: str, message: str, status: int) -> None:
        self._json({"error": {"code": code, "message": message, "resource": {}}}, status=status)

    def _bytes(self, value: bytes, content_type: str, *, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)

    def _static(self, name: str) -> None:
        clean = PurePosixPath(name)
        if clean.is_absolute() or ".." in clean.parts or str(clean) in {"", "."}:
            raise ObservatoryProblem("not_found", "resource not found", status=404)
        resource = files("lanscoder.observability.web").joinpath("static", *clean.parts)
        if not resource.is_file():
            raise ObservatoryProblem("not_found", "resource not found", status=404)
        content_type = mimetypes.guess_type(str(clean))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._bytes(resource.read_bytes(), content_type)

    def log_message(self, format: str, *args: object) -> None:
        return


def _component(path: str, index: int) -> str:
    parts = path.split("/")
    component_index = index + 1
    if len(parts) <= component_index or not parts[component_index]:
        raise ObservatoryProblem("not_found", "resource not found", status=404)
    return parts[component_index]


def _is_exact(path: str, prefix: str, expected_parts: int) -> bool:
    parts = path.split("/")
    return path.startswith(prefix) and len(parts) == expected_parts + 1 and all(parts[1:])


def _single_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.pop(name, [])
    if len(values) > 1 or query:
        raise ObservatoryProblem("invalid_query", "invalid query parameters", status=400)
    return values[0] if values else None
