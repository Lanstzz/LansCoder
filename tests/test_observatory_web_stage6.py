from __future__ import annotations

import json
from http.client import HTTPConnection
from importlib.resources import files
from pathlib import Path

from lanscoder.journal import JournalStore
from lanscoder.observability.web.server import ObservatoryServer, launch_browser
from lanscoder.storage import LansCoderPaths, PayloadStore


def _journal(tmp_path: Path) -> tuple[LansCoderPaths, str, str]:
    paths = LansCoderPaths(storage_root=tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append("session.created", {"root_branch_id": "brn_root", "kind": "primary", "project_id": "project-a", "title": "Web"}, branch_id="brn_root")
    journal.append(
        "trace.started", {"provider": "fake", "model": "model-a", "input": "original question", "metadata": {"project_id": "project-a"}, "tags": ["smoke"]}, trace_id="trc_one", branch_id="brn_root"
    )
    journal.append(
        "observation.started",
        {"observation_type": "generation", "provider": "fake", "model": "model-a", "parameters": {"temperature": 0.2}},
        trace_id="trc_one",
        observation_id="obs_one",
        branch_id="brn_root",
    )
    journal.append(
        "observation.ended", {"outcome": "succeeded", "usage": {"total_tokens": 3, "usage_details": {"cached_tokens": 1}}}, trace_id="trc_one", observation_id="obs_one", branch_id="brn_root"
    )
    journal.append("trace.ended", {"status": "completed", "outcome": "succeeded", "final_output": "hello"}, trace_id="trc_one", branch_id="brn_root")
    return paths, "sess_web", "trc_one"


def _request(server: ObservatoryServer, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    host, port = server.address
    connection = HTTPConnection(host, port, timeout=3)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, headers, body


def test_foreground_serve_forever_runs_one_http_loop(tmp_path: Path, monkeypatch) -> None:
    server = ObservatoryServer(LansCoderPaths(storage_root=tmp_path))

    class FakeHTTPD:
        server_address = ("127.0.0.1", 12345)

        def __init__(self) -> None:
            self.calls = 0

        def serve_forever(self, *, poll_interval: float) -> None:
            self.calls += 1

        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    fake = FakeHTTPD()
    monkeypatch.setattr("lanscoder.observability.web.server._BoundHTTPServer", lambda *args, **kwargs: fake)
    server.serve_forever()
    assert fake.calls == 1
    server.shutdown()


def test_web_api_lists_filters_and_details_traces(tmp_path: Path) -> None:
    paths, session_id, trace_id = _journal(tmp_path)
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", "/api/v1/traces?limit=1&metadata.project_id=project-a")
        assert status == 200
        payload = json.loads(body)
        assert payload["items"][0]["trace_id"] == trace_id
        assert payload["next_cursor"] is None

        status, _, body = _request(server, "GET", f"/api/v1/traces/{trace_id}")
        assert status == 200
        detail = json.loads(body)
        assert detail["final_output"] == "hello"
        assert detail["observations"][0]["data"]["usage"]["usage_details"]["cached_tokens"] == 1

        status, _, body = _request(server, "GET", f"/api/v1/sessions/{session_id}/replay")
        assert status == 200
        assert json.loads(body)["active_branch_id"] == "brn_root"
        status, _, body = _request(server, "GET", "/api/v1/sessions")
        assert status == 200
        assert json.loads(body)["items"][0]["session_id"] == session_id
    finally:
        server.shutdown()


def test_web_server_is_loopback_get_only_and_serves_static_assets(tmp_path: Path) -> None:
    paths, _, _ = _journal(tmp_path)
    server = ObservatoryServer(paths).start()
    try:
        assert server.address[0] == "127.0.0.1"
        assert server.address[1] > 0
        assert _request(server, "GET", "/healthz")[0] == 200
        assert _request(server, "GET", "/")[0] == 200
        assert b"Trace Explorer" in _request(server, "GET", "/")[2]
        assert _request(server, "POST", "/api/v1/traces")[0] == 405
        assert _request(server, "OPTIONS", "/api/v1/traces")[0] == 405
        assert _request(server, "TRACE", "/api/v1/traces")[0] == 405
        assert _request(server, "GET", "/api/v1/nope")[0] == 404
    finally:
        server.shutdown()


def test_payload_endpoint_reads_content_addressed_payload(tmp_path: Path) -> None:
    paths, _, _ = _journal(tmp_path)
    reference = PayloadStore(paths).put(b"evidence", media_type="text/plain")
    JournalStore(paths, "sess_web").append("evidence.saved", {"output_ref": reference.to_dict()})
    server = ObservatoryServer(paths).start()
    try:
        status, headers, body = _request(server, "GET", f"/api/v1/payloads/{reference.sha256}")
        assert status == 200
        assert headers["content-type"].startswith("text/plain")
        assert body == b"evidence"
        assert _request(server, "GET", f"/api/v1/payloads/{reference.sha256}/extra")[0] == 404
    finally:
        server.shutdown()


def test_exact_api_resources_reject_trailing_slashes(tmp_path: Path) -> None:
    paths, session_id, trace_id = _journal(tmp_path)
    reference = PayloadStore(paths).put(b"evidence", media_type="text/plain")
    server = ObservatoryServer(paths).start()
    try:
        assert _request(server, "GET", f"/api/v1/traces/{trace_id}/")[0] == 404
        assert _request(server, "GET", f"/api/v1/payloads/{reference.sha256}/")[0] == 404
        assert _request(server, "GET", f"/api/v1/sessions/{session_id}/replay/")[0] == 404
    finally:
        server.shutdown()


def test_api_detail_exposes_input_metadata_and_evidence_payload(tmp_path: Path) -> None:
    paths, _, trace_id = _journal(tmp_path)
    reference = PayloadStore(paths).put_json({"answer": 42})
    JournalStore(paths, "sess_web").append("evidence.saved", {"payload_ref": reference.to_dict()}, trace_id=trace_id, branch_id="brn_root")
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", f"/api/v1/traces/{trace_id}")
        assert status == 200
        detail = json.loads(body)
        assert detail["input"] == "original question"
        assert detail["metadata"]["project_id"] == "project-a"
        assert detail["evidence_payloads"][0]["resolved"] is True
        assert detail["evidence_payloads"][0]["value"] == {"answer": 42}
        assert _request(server, "GET", f"/api/v1/traces/{trace_id}/extra")[0] == 404
        assert _request(server, "GET", "/api/v1/sessions/sess_web/replay/extra")[0] == 404
    finally:
        server.shutdown()


def test_session_replay_collects_trace_ids_from_link_data(tmp_path: Path) -> None:
    paths, session_id, trace_id = _journal(tmp_path)
    journal = JournalStore(paths, session_id)
    journal.append(
        "trace.linked",
        {
            "parent_trace_id": trace_id,
            "child_trace_id": "trc_cross_session",
            "background_trace_id": "trc_background",
            "links": [{"related_trace_id": "trc_nested"}],
        },
        trace_id="trc_cross_session",
        branch_id="brn_root",
    )
    service = ObservatoryServer(paths).query_service
    replay = service.replay_session(session_id)
    assert replay["linked_trace_ids"] == ["trc_background", "trc_cross_session", "trc_nested", trace_id]


def test_trace_cursor_pagination_and_browser_fallback(tmp_path: Path, monkeypatch) -> None:
    paths, _, _ = _journal(tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append("trace.started", {"provider": "fake", "model": "model-b"}, trace_id="trc_two", branch_id="brn_root")
    journal.append("trace.ended", {"status": "failed", "error": {"message": "nope"}}, trace_id="trc_two", branch_id="brn_root")
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", "/api/v1/traces?limit=1&has_error=true")
        assert status == 200
        assert len(json.loads(body)["items"]) == 1
        status, _, body = _request(server, "GET", "/api/v1/traces?limit=1")
        assert status == 200
        first = json.loads(body)
        assert len(first["items"]) == 1
        assert first["next_cursor"] is not None
        status, _, body = _request(server, "GET", f"/api/v1/traces?limit=1&cursor={first['next_cursor']}")
        assert status == 200
        assert len(json.loads(body)["items"]) == 1
    finally:
        server.shutdown()

    monkeypatch.setattr("lanscoder.observability.web.server.webbrowser.open", lambda url: (_ for _ in ()).throw(OSError("no browser")))
    assert launch_browser("http://127.0.0.1:9/") == "http://127.0.0.1:9/"


def test_static_resources_are_package_readable() -> None:
    package = files("lanscoder.observability.web").joinpath("static")
    assert package.joinpath("index.html").read_text(encoding="utf-8").find("Trace Explorer") >= 0
    app = package.joinpath("app.js").read_text(encoding="utf-8")
    assert package.joinpath("app.js").is_file()
    assert "trace.trace_metadata" in app
    assert "trace.evidence_payloads" in app
    assert "window.location.pathname" in app
