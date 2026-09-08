from __future__ import annotations

import json
import subprocess
from http.client import HTTPConnection
from importlib.resources import files
from pathlib import Path

from lanscoder.journal import JournalStore
from lanscoder.observability.web import api as web_api
from lanscoder.observability.web.server import ObservatoryServer, launch_browser
from lanscoder.storage import LansCoderPaths, PayloadStore


def _journal(tmp_path: Path) -> tuple[LansCoderPaths, str, str]:
    paths = LansCoderPaths(storage_root=tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append(
        "session.created",
        {"root_branch_id": "brn_root", "kind": "primary", "project_id": "project-a", "title": "Web"},
        branch_id="brn_root",
    )
    journal.append(
        "message.appended",
        {"role": "user", "content": "original question", "message_id": "msg_one"},
        branch_id="brn_root",
    )
    journal.append(
        "trace.started",
        {"provider": "fake", "model": "model-a", "input": "original question", "metadata": {"project_id": "project-a"}, "tags": ["smoke"]},
        trace_id="trc_one",
        branch_id="brn_root",
    )
    journal.append(
        "observation.started",
        {
            "observation_type": "generation",
            "provider": "fake",
            "model": "model-a",
            "parameters": {"temperature": 0.2},
        },
        trace_id="trc_one",
        observation_id="obs_one",
        branch_id="brn_root",
    )
    journal.append(
        "observation.ended",
        {"outcome": "succeeded", "usage": {"total_tokens": 3, "usage_details": {"cached_tokens": 1}}},
        trace_id="trc_one",
        observation_id="obs_one",
        branch_id="brn_root",
    )
    journal.append(
        "trace.ended",
        {"status": "completed", "outcome": "succeeded", "final_output": "hello"},
        trace_id="trc_one",
        branch_id="brn_root",
    )
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


def _problem(body: bytes) -> dict[str, object]:
    return json.loads(body)["error"]


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


def test_trace_endpoints_return_stable_presentation_dtos(tmp_path: Path) -> None:
    paths, _, trace_id = _journal(tmp_path)
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", "/api/v1/traces?metadata.project_id=%22project-a%22")
        assert status == 200
        listing = json.loads(body)
        assert listing["items"][0]["trace_id"] == trace_id
        assert listing["items"][0]["input_preview"] == "original question"
        assert listing["unfiltered_total"] == 1
        assert listing["empty_reason"] is None
        assert listing["diagnostics"] == []

        status, _, body = _request(server, "GET", f"/api/v1/traces/{trace_id}")
        assert status == 200
        detail = json.loads(body)
        observation = detail["observations"][0]
        assert observation["type"] == "generation"
        assert observation["display_name"] == "Generation · model-a"
        assert observation["incomplete"] is False
        assert observation["overview"]["usage"]["usage_details"]["cached_tokens"] == 1
        assert "data" not in observation
        assert set(observation["raw"]["started_event"]) == {"event_id", "sequence", "occurred_at", "data"}
        assert detail["final_output"] == "hello"
        assert detail["evidence_completeness"] == {"complete": True, "incomplete": False}
    finally:
        server.shutdown()


def test_trace_query_rejects_unknown_invalid_and_mismatched_cursors(tmp_path: Path) -> None:
    paths, _, _ = _journal(tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append("trace.started", {"provider": "fake", "model": "model-b"}, trace_id="trc_two", branch_id="brn_root")
    journal.append("trace.ended", {"status": "failed", "error": {"message": "nope"}}, trace_id="trc_two", branch_id="brn_root")
    server = ObservatoryServer(paths).start()
    try:
        for query in (
            "session=sess_web",
            "has_error=yes",
            "limit=201",
            "metadata.project_id=project-a",
            "metadata.unbounded=%22value%22",
            "from=not-a-time",
        ):
            status, _, body = _request(server, "GET", f"/api/v1/traces?{query}")
            assert status == 400
            assert _problem(body)["code"] == "invalid_query"

        status, _, body = _request(server, "GET", "/api/v1/traces?limit=1")
        assert status == 200
        cursor = json.loads(body)["next_cursor"]
        assert cursor is not None
        status, _, body = _request(server, "GET", f"/api/v1/traces?limit=200&cursor={cursor}")
        assert status == 200
        status, _, body = _request(server, "GET", f"/api/v1/traces?status=failed&cursor={cursor}")
        assert status == 400
        assert _problem(body)["code"] == "invalid_cursor"
    finally:
        server.shutdown()


def test_trace_query_matches_repeated_metadata_values_with_or(tmp_path: Path) -> None:
    paths, _, trace_id = _journal(tmp_path)
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(
            server,
            "GET",
            "/api/v1/traces?metadata.project_id=%22other%22&metadata.project_id=%22project-a%22",
        )
        assert status == 200
        assert [item["trace_id"] for item in json.loads(body)["items"]] == [trace_id]
    finally:
        server.shutdown()


def test_payload_descriptor_binds_expected_size_and_problem_classification(tmp_path: Path) -> None:
    paths, _, trace_id = _journal(tmp_path)
    reference = PayloadStore(paths).put_json({"answer": 42})
    journal = JournalStore(paths, "sess_web")
    journal.append(
        "observation.started",
        {"observation_type": "generation", "normalized_request": {"payload_ref": reference.to_dict()}},
        trace_id=trace_id,
        observation_id="obs_payload",
        branch_id="brn_root",
    )
    server = ObservatoryServer(paths).start()
    try:
        detail = json.loads(_request(server, "GET", f"/api/v1/traces/{trace_id}")[2])
        descriptor = detail["observations"][1]["input"]
        assert descriptor["url"].endswith(f"{reference.sha256}?size_bytes={reference.size_bytes}")
        assert "value" not in descriptor
        status, headers, body = _request(server, "GET", descriptor["url"])
        assert status == 200
        assert headers["content-type"].startswith("application/json")
        assert json.loads(body) == {"answer": 42}
        status, _, body = _request(server, "GET", f"/api/v1/payloads/{reference.sha256}?size_bytes=0")
        assert status == 409
        assert _problem(body)["code"] == "payload_corrupt"
        status, _, body = _request(server, "GET", "/api/v1/payloads/" + "0" * 64 + "?size_bytes=1")
        assert status == 404
        assert _problem(body)["code"] == "payload_missing"
    finally:
        server.shutdown()


def test_session_replay_uses_selected_branch_projection_and_does_not_guess_trace_links(tmp_path: Path) -> None:
    paths, session_id, trace_id = _journal(tmp_path)
    journal = JournalStore(paths, session_id)
    journal.append(
        "session.recalled",
        {"new_branch_id": "brn_history", "parent_branch_id": "brn_root", "base_sequence": 2},
        branch_id="brn_history",
    )
    journal.append(
        "message.appended",
        {"role": "assistant", "content": "historical response", "message_id": "msg_history"},
        branch_id="brn_history",
    )
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", f"/api/v1/sessions/{session_id}/replay?branch=brn_history")
        assert status == 200
        replay = json.loads(body)
        assert replay["selected_branch_id"] == "brn_history"
        assert [item["content"] for item in replay["items"]] == ["original question", "historical response"]
        assert replay["items"][0]["trace_id"] is None
        assert trace_id in replay["linked_trace_ids"]
        status, _, body = _request(server, "GET", f"/api/v1/sessions/{session_id}/replay?branch=nope")
        assert status == 404
        assert _problem(body)["code"] == "not_found"
    finally:
        server.shutdown()


def test_session_replay_projects_real_message_parts_and_preserves_explicit_trace_id(tmp_path: Path) -> None:
    paths, session_id, trace_id = _journal(tmp_path)
    journal = JournalStore(paths, session_id)
    journal.append(
        "message.appended",
        {
            "role": "assistant",
            "parts": [{"kind": "text", "content": "part-backed response"}],
            "trace_id": trace_id,
        },
        branch_id="brn_root",
    )
    server = ObservatoryServer(paths).start()
    try:
        replay = json.loads(_request(server, "GET", f"/api/v1/sessions/{session_id}/replay")[2])
        assert replay["items"][-1]["content"] == "part-backed response"
        assert replay["items"][-1]["trace_id"] == trace_id
        sessions = json.loads(_request(server, "GET", "/api/v1/sessions")[2])
        assert sessions["items"][0]["latest_user_input"] == "original question"
    finally:
        server.shutdown()


def test_background_detached_requires_matching_scheduled_job(tmp_path: Path) -> None:
    paths, _, _ = _journal(tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append(
        "trace.started",
        {"parent_trace_id": "trc_one", "job_id": "bg_one"},
        trace_id="trc_background",
        branch_id="brn_root",
    )
    journal.append(
        "trace.linked",
        {"parent_trace_id": "trc_one", "child_trace_id": "trc_background", "relation": "background", "job_id": "bg_one"},
        trace_id="trc_background",
        branch_id="brn_root",
    )
    journal.append("background.scheduled", {"job_id": "bg_one", "parent_trace_id": "trc_one"}, branch_id="brn_root")
    journal.append(
        "background.completed",
        {"job_id": "bg_one", "background_trace_id": "trc_background", "status": "completed", "detached_from_active_branch": True},
        branch_id="brn_root",
    )
    server = ObservatoryServer(paths).start()
    try:
        detail = json.loads(_request(server, "GET", "/api/v1/traces/trc_one")[2])
        assert detail["relations"][0]["detached"] is True
        listing = json.loads(_request(server, "GET", "/api/v1/traces")[2])
        background = next(item for item in listing["items"] if item["trace_id"] == "trc_background")
        assert background["detached"] is True
    finally:
        server.shutdown()


def test_background_lifecycle_without_trace_link_remains_trace_relation(tmp_path: Path) -> None:
    paths, _, _ = _journal(tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append(
        "background.scheduled",
        {"job_id": "bg_only", "parent_trace_id": "trc_one", "parent_observation_id": "obs_one"},
        branch_id="brn_root",
    )
    journal.append(
        "background.completed",
        {"job_id": "bg_only", "background_trace_id": "trc_background_only", "status": "completed"},
        branch_id="brn_root",
    )
    server = ObservatoryServer(paths).start()
    try:
        detail = json.loads(_request(server, "GET", "/api/v1/traces/trc_one")[2])
        relation = detail["observations"][0]["relations"][0]
        assert relation["linked_trace_id"] == "trc_background_only"
        assert relation["completion_status"] == "completed"
    finally:
        server.shutdown()


def test_background_lifecycle_enriches_matching_trace_link_relation(tmp_path: Path) -> None:
    paths, _, _ = _journal(tmp_path)
    journal = JournalStore(paths, "sess_web")
    journal.append(
        "trace.linked",
        {
            "parent_trace_id": "trc_one",
            "child_trace_id": "trc_background",
            "relation": "background",
            "job_id": "bg_mixed",
        },
        trace_id="trc_background",
        branch_id="brn_root",
    )
    journal.append(
        "background.scheduled",
        {"job_id": "bg_mixed", "parent_trace_id": "trc_one", "parent_observation_id": "obs_one"},
        branch_id="brn_root",
    )
    journal.append(
        "background.completed",
        {"job_id": "bg_mixed", "background_trace_id": "trc_background", "status": "completed"},
        branch_id="brn_root",
    )
    server = ObservatoryServer(paths).start()
    try:
        detail = json.loads(_request(server, "GET", "/api/v1/traces/trc_one")[2])
        relation = detail["observations"][0]["relations"][0]
        assert relation["linked_trace_id"] == "trc_background"
        assert relation["job_id"] == "bg_mixed"
        assert relation["dispatch_status"] == "scheduled"
        assert relation["completion_status"] == "completed"
    finally:
        server.shutdown()


def test_orphan_and_cycle_observation_diagnostics_do_not_make_evidence_incomplete(tmp_path: Path) -> None:
    paths, _, trace_id = _journal(tmp_path)
    journal = JournalStore(paths, "sess_web")
    for observation_id, parent_observation_id in (("obs_orphan", "obs_missing"), ("obs_cycle_a", "obs_cycle_b"), ("obs_cycle_b", "obs_cycle_a")):
        journal.append(
            "observation.started",
            {"observation_type": "event"},
            trace_id=trace_id,
            observation_id=observation_id,
            parent_observation_id=parent_observation_id,
            branch_id="brn_root",
        )
        journal.append(
            "observation.ended",
            {"outcome": "succeeded"},
            trace_id=trace_id,
            observation_id=observation_id,
            parent_observation_id=parent_observation_id,
            branch_id="brn_root",
        )
    server = ObservatoryServer(paths).start()
    try:
        detail = json.loads(_request(server, "GET", f"/api/v1/traces/{trace_id}")[2])
        observations = {item["observation_id"]: item for item in detail["observations"]}
        assert observations["obs_orphan"]["diagnostics"] == ["orphan_parent"]
        assert observations["obs_orphan"]["incomplete"] is False
        assert "parent_cycle" in observations["obs_cycle_a"]["diagnostics"]
        assert observations["obs_cycle_a"]["incomplete"] is False
    finally:
        server.shutdown()


def test_trace_list_falls_back_from_a_failed_materialized_index(tmp_path: Path, monkeypatch) -> None:
    paths, _, trace_id = _journal(tmp_path)
    monkeypatch.setattr(
        web_api.JournalTraceIndex,
        "list_summaries",
        lambda self: (_ for _ in ()).throw(OSError("index unavailable")),
    )
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", "/api/v1/traces")
        assert status == 200
        listing = json.loads(body)
        assert [item["trace_id"] for item in listing["items"]] == [trace_id]
        assert listing["diagnostics"][0]["code"] == "trace_index_unavailable"
    finally:
        server.shutdown()


def test_corrupt_sessions_are_excluded_and_reported(tmp_path: Path) -> None:
    paths, session_id, _ = _journal(tmp_path)
    paths.session("sess_corrupt").parent.mkdir(parents=True, exist_ok=True)
    paths.session("sess_corrupt").write_text("{bad json}\n", encoding="utf-8")
    server = ObservatoryServer(paths).start()
    try:
        status, _, body = _request(server, "GET", "/api/v1/sessions")
        assert status == 200
        sessions = json.loads(body)
        assert [item["session_id"] for item in sessions["items"]] == [session_id]
        assert sessions["diagnostics"][0]["session_id"] == "sess_corrupt"
        status, _, body = _request(server, "GET", "/api/v1/sessions/sess_corrupt/replay")
        assert status == 409
        assert _problem(body)["code"] == "journal_corrupt"
    finally:
        server.shutdown()


def test_server_uses_exact_read_only_routes_and_json_problems(tmp_path: Path) -> None:
    paths, session_id, trace_id = _journal(tmp_path)
    server = ObservatoryServer(paths).start()
    try:
        assert server.address[0] == "127.0.0.1"
        assert _request(server, "GET", "/healthz")[0] == 200
        for path in ("/", "/traces", f"/traces/{trace_id}", "/sessions", f"/sessions/{session_id}", "/static/index.html"):
            assert _request(server, "GET", path)[0] == 200
        for path in ("/traces/", "/traces/a/b", "/sessions/", "/sessions/a/b", "/api/v1/traces/", f"/api/v1/traces/{trace_id}/"):
            status, headers, body = _request(server, "GET", path)
            assert status == 404
            assert headers["content-type"].startswith("application/json")
            assert _problem(body)["code"] == "not_found"
        assert _request(server, "POST", "/api/v1/traces")[0] == 405
    finally:
        server.shutdown()


def test_static_resources_are_package_readable() -> None:
    package = files("lanscoder.observability.web").joinpath("static")
    assert package.joinpath("index.html").is_file()
    assert package.joinpath("app.js").is_file()
    assert package.joinpath("app.css").is_file()


def test_static_application_contract_uses_the_app_root_and_stable_dtos() -> None:
    package = files("lanscoder.observability.web").joinpath("static")
    html = package.joinpath("index.html").read_text(encoding="utf-8")
    script = package.joinpath("app.js").read_text(encoding="utf-8")
    stylesheet = package.joinpath("app.css").read_text(encoding="utf-8")

    assert 'id="app"' in html
    assert "document.createElement" in script
    assert ".textContent" in script
    assert ".innerHTML" not in script
    assert "history.pushState" in script
    assert "popstate" in script
    assert "AbortController" in script
    assert "setTimeout" in script
    assert "clearTimeout" in script
    assert "observations" in script
    assert "raw_events" in script
    harness = Path(__file__).with_name("explorer_filters_dom_harness.js")
    result = subprocess.run(
        ["node", str(harness), str(package.joinpath("app.js"))],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "trace.timeline" not in script
    assert "branch.events" not in script
    assert "trace_metadata" not in script
    assert "evidence_payloads" not in script
    assert 'querySelector("#traces")' not in script
    for view in ("renderExplorer", "renderDetail", "renderSessions", "renderReplay"):
        assert view in script
    for filter_name in (
        "project",
        "session_id",
        "status",
        "model",
        "provider",
        "tool",
        "tag",
        "has_error",
        "min_duration_ms",
        "max_duration_ms",
        "min_tokens",
        "max_tokens",
        "min_observation_count",
        "max_observation_count",
        "metadata_key",
        "metadata_value",
    ):
        assert filter_name in script
    assert "View payload" in script
    assert "Live data may shift between pages." in script
    assert "Historical · read-only" in script
    assert "Branch:" in script
    assert 'item.type === "agent" && item.parent_observation_id === null' in script
    assert "activity" in script
    assert "--blue: #002fa7" in stylesheet
    assert "border-left: 3px solid transparent" in stylesheet
    assert '.tab[aria-selected="true"] { border-left-color: var(--blue);' in stylesheet
    assert "linear-gradient" not in stylesheet
    assert "box-shadow" not in stylesheet


def test_trace_detail_heading_renders_trace_id_as_code() -> None:
    package = files("lanscoder.observability.web").joinpath("static")
    harness = Path(__file__).with_name("trace_heading_dom_harness.js")
    result = subprocess.run(
        ["node", str(harness), str(package.joinpath("app.js"))],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_browser_launcher_returns_a_manual_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "lanscoder.observability.web.server.webbrowser.open",
        lambda url: (_ for _ in ()).throw(OSError("no browser")),
    )
    assert launch_browser("http://127.0.0.1:9/") == "http://127.0.0.1:9/"
