from pathlib import Path

from lanscoder.context.archive import ToolResultArchive
from lanscoder.context.checkpoint import Checkpoint
from lanscoder.context.compaction import CompactionPipeline, CompactionRequest
from lanscoder.context.identity import session_view_fingerprint
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.context.token_budget import estimate_text_tokens
from lanscoder.context.tool_sequence import validate_tool_call_sequence
from lanscoder.context.versions import COMPACTION_STRATEGY_VERSION


def _message(
    message_id: str,
    *,
    role: str = "user",
    kind: str = "text",
    content: str = "content",
    created_turn: int = 10,
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    part_metadata = {
        "created_turn": created_turn,
    }
    if metadata:
        part_metadata.update(metadata)
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind=kind,
                content=content,
                metadata=part_metadata,
            )
        ],
    )


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> AgentMessage:
    message_id = f"msg_call_{call_id}"
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id=f"part_call_{call_id}",
                message_id=message_id,
                kind="tool_call",
                content="",
                metadata={
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "arguments": arguments,
                },
            )
        ],
    )


def _tool_result(
    call_id: str,
    name: str,
    *,
    content: str,
    data: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    message_id = f"msg_result_{call_id}"
    result_metadata: dict[str, object] = {
        "tool_call_id": call_id,
        "tool_name": name,
        "ok": True,
        "data": data or {},
    }
    if metadata:
        result_metadata.update(metadata)
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="tool",
        parts=[
            MessagePart(
                id=f"part_result_{call_id}",
                message_id=message_id,
                kind="tool_result",
                content=content,
                metadata=result_metadata,
            )
        ],
    )


def _request(
    *,
    view: SessionView,
    target_tokens: int,
    current_turn: int,
    estimate_tokens=None,
    consumed_tool_result_part_ids: frozenset[str] | None = None,
    **kwargs,
) -> CompactionRequest:
    if estimate_tokens is None:

        def estimate_tokens(candidate):
            return sum(estimate_text_tokens(part.content) for message in candidate.messages for part in message.parts)

    if consumed_tool_result_part_ids is None:
        consumed_tool_result_part_ids = frozenset(part.id for message in view.messages for part in message.parts if part.kind == "tool_result")
    return CompactionRequest(
        view=view,
        target_tokens=target_tokens,
        current_turn=current_turn,
        estimate_tokens=estimate_tokens,
        consumed_tool_result_part_ids=consumed_tool_result_part_ids,
        **kwargs,
    )


def _derived_tool_result_view(*, content: str) -> tuple[SessionView, MessagePart]:
    result_message = _tool_result(
        "consumption_guard",
        "shell",
        content=content,
        data={"command": "pytest -q", "exit_code": 1},
    )
    return (
        SessionView(
            session_id="sess_test",
            messages=[
                _tool_call("consumption_guard", "shell", {"command": "pytest -q"}),
                result_message,
            ],
        ),
        result_message.parts[0],
    )


def test_pipeline_uses_request_estimator_instead_of_raw_ledger(tmp_path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_old", content="x" * 40_000), _message("msg_tail", content="tail")],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="short",
                tail_start_message_id="msg_tail",
                covered_until_message_id="msg_old",
                source_fingerprint="fp_1",
            )
        ],
    )
    estimates = []

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100,
            current_turn=1,
            estimate_tokens=lambda candidate: estimates.append(candidate) or 5,
            consumed_tool_result_part_ids=frozenset(),
        )
    )

    assert result.event.noop is True
    assert result.event.before_tokens == 5
    assert estimates


def test_unconsumed_derived_result_is_not_l1_or_l2_candidate(tmp_path) -> None:
    view, part = _derived_tool_result_view(content="FAILED\n" + "x" * 8_000)

    def estimate(candidate: SessionView) -> int:
        return sum(estimate_text_tokens(item.content) for message in candidate.messages for item in message.parts)

    protected = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            estimate_tokens=estimate,
            consumed_tool_result_part_ids=frozenset(),
        )
    )
    consumed = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            estimate_tokens=estimate,
            consumed_tool_result_part_ids=frozenset({part.id}),
        )
    )

    assert protected.event.changed_parts == 0
    assert consumed.event.changed_parts > 0
    assert consumed.event.archive_ids


def test_session_view_fingerprint_tracks_persisted_message_content() -> None:
    original = SessionView(session_id="sess_test", messages=[_message("msg_1", content="original")])
    same = SessionView(session_id="sess_test", messages=[_message("msg_1", content="original")])
    changed = SessionView(session_id="sess_test", messages=[_message("msg_1", content="changed")])

    assert session_view_fingerprint(original) == session_view_fingerprint(same)
    assert session_view_fingerprint(original) != session_view_fingerprint(changed)
    assert len(session_view_fingerprint(original)) == 24


def test_compaction_event_records_full_schema(tmp_path: Path) -> None:
    view, part = _derived_tool_result_view(content="x" * 40_000)

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert result.event.event_version == "v2"
    assert result.event.strategy_version == COMPACTION_STRATEGY_VERSION
    assert result.event.reason in {"l1", "l2", "not_reached"}
    assert result.event.target_tokens == 1
    assert result.event.source_part_ids == [part.id]
    assert result.event.output_part_ids == [part.id]
    assert result.event.checkpoint_id is None
    assert result.event.llm_used is False
    assert result.event.success is True
    assert result.event.error is None
    assert result.event.created_at.endswith("Z")


def test_l1_routes_derived_search_and_stores_raw_backing(tmp_path: Path) -> None:
    raw_content = "\n".join(f"lanscoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 160))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("search_l1", "grep", {"pattern": "function"}),
            _tool_result("search_l1", "grep", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    part = result.view.messages[1].parts[0]
    assert part.metadata["compaction_state"] == "l2_route_compacted"
    assert part.metadata["compacted_by"] == "l2_search_results"
    assert part.metadata["lifecycle"] == "derived"
    assert part.metadata["tool_call_id"] == "search_l1"
    assert part.metadata["replacement_tokens"] < part.metadata["original_tokens"]
    record, backed = ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])
    assert backed == raw_content
    assert record.content_sha256 == part.metadata["original_content_sha256"]


def test_l1_never_routes_fresh_source_and_does_not_create_backing(tmp_path: Path) -> None:
    raw_content = "source line\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_l1_fresh", "view", {"path": "lanscoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "view_l1_fresh",
                "view",
                content=raw_content,
                data={
                    "path": "lanscoder/context.py",
                    "start_line": 1,
                    "end_line": 500,
                    "total_lines": 2_000,
                    "truncated": True,
                },
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    assert result.view.messages[1].parts[0].content == raw_content
    assert result.view.messages[1].parts[0].metadata.get("compaction_state") is None
    assert not (tmp_path / "archives").exists()


def test_l1_skips_when_router_has_no_strictly_smaller_candidate(tmp_path: Path) -> None:
    raw_content = "small derived result"
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("small_l1", "shell", {"command": "echo small"}),
            _tool_result("small_l1", "shell", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    assert result.view.messages[1].parts[0].content == raw_content
    assert not (tmp_path / "archives").exists()


def test_l1_routes_build_and_diff_derived_results_with_raw_backing(tmp_path: Path) -> None:
    build_raw = "\n".join(
        [
            "pytest tests/test_context.py",
            *[f"normal test output line {line}" for line in range(1, 130)],
            "tests/test_context.py::test_resume FAILED",
            "Traceback (most recent call last):",
            '  File "tests/test_context.py", line 33, in test_resume',
            "AssertionError",
            "1 failed, 12 passed in 1.23s",
        ]
    )
    diff_raw = "\n".join(
        [
            "diff --git a/lanscoder/app.py b/lanscoder/app.py",
            "--- a/lanscoder/app.py",
            "+++ b/lanscoder/app.py",
            "@@ -1,4 +1,4 @@",
            *[f" context {line}" for line in range(1, 100)],
            "-old line",
            "+new line",
        ]
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("build_l1", "pytest", {"command": "pytest"}),
            _tool_result("build_l1", "pytest", content=build_raw),
            _tool_call("diff_l1", "git_diff", {"path": "lanscoder/app.py"}),
            _tool_result("diff_l1", "git_diff", content=diff_raw),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    build_part = result.view.messages[1].parts[0]
    diff_part = result.view.messages[3].parts[0]
    assert build_part.metadata["compacted_by"] == "l2_build_output"
    assert build_part.metadata["build_omitted_lines"] > 0
    assert diff_part.metadata["compacted_by"] == "l2_git_diff"
    assert diff_part.metadata["diff_context_lines_omitted"] > 0
    assert ToolResultArchive(tmp_path).read("sess_test", build_part.metadata["archive_id"])[1] == build_raw
    assert ToolResultArchive(tmp_path).read("sess_test", diff_part.metadata["archive_id"])[1] == diff_raw
    validate_tool_call_sequence(result.view.messages)


def test_l1_then_l2_uses_existing_raw_backing_and_is_idempotent(tmp_path: Path) -> None:
    raw_content = "\n".join(f"lanscoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 160))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("l1_l2", "grep", {"pattern": "function"}),
            _tool_result("l1_l2", "grep", content=raw_content),
        ],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = _request(
        view=view,
        target_tokens=1,
        current_turn=10,
        enabled_levels=("l1", "l2"),
        l2_result_target_tokens=20,
    )

    first = pipeline.compact(request)
    archived = first.view.messages[1].parts[0]
    second = pipeline.compact(
        _request(
            view=first.view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1", "l2"),
            l2_result_target_tokens=20,
        )
    )

    assert archived.metadata["compaction_state"] == "archived"
    assert archived.metadata["compacted_by"] == "l3_archive"
    assert ToolResultArchive(tmp_path).read("sess_test", archived.metadata["archive_id"])[1] == raw_content
    assert second.event.changed_parts == 0
    assert len(list((tmp_path / "archives" / "sess_test").glob("*.txt"))) == 1
    validate_tool_call_sequence(first.view.messages)
    validate_tool_call_sequence(second.view.messages)


def test_per_result_pressure_runs_l1_then_l2_below_total_budget(tmp_path: Path) -> None:
    raw_content = "\n".join(f"lanscoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 160))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pressure_l1_l2", "grep", {"pattern": "function"}),
            _tool_result("pressure_l1_l2", "grep", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l1", "l2"),
            l2_result_target_tokens=1,
        )
    )

    part = result.view.messages[1].parts[0]
    assert result.event.levels_attempted == ["l1", "l2"]
    assert result.event.changed_parts == 2
    assert result.event.replacements[0]["replacement_part"]["metadata"]["compacted_by"] == "l2_search_results"
    assert part.metadata["compaction_state"] == "archived"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == raw_content


def test_per_result_pressure_does_not_bypass_fresh_source_noop(tmp_path: Path) -> None:
    raw_content = "source line\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pressure_fresh", "view", {"path": "lanscoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "pressure_fresh",
                "view",
                content=raw_content,
                data={
                    "path": "lanscoder/context.py",
                    "start_line": 1,
                    "end_line": 500,
                    "total_lines": 2_000,
                    "truncated": True,
                },
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l1", "l2"),
            l2_result_target_tokens=1,
        )
    )

    assert result.event.levels_attempted == []
    assert result.event.stopped_at == "already_within_budget"
    assert result.view.messages[1].parts[0].content == raw_content
    assert not (tmp_path / "archives").exists()


def test_per_result_pressure_archives_raw_derived_below_total_budget(tmp_path: Path) -> None:
    raw_content = "plain shell output\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pressure_raw", "shell", {"command": "long-command"}),
            _tool_result("pressure_raw", "shell", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l1", "l2"),
            l2_result_target_tokens=1,
        )
    )

    part = result.view.messages[1].parts[0]
    assert result.event.levels_attempted == ["l1", "l2"]
    assert part.metadata["compaction_state"] == "archived"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == raw_content


def test_l2_archives_raw_derived_result_when_over_budget(tmp_path: Path) -> None:
    raw_content = "plain shell output\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("raw_l2", "shell", {"command": "long-command"}),
            _tool_result("raw_l2", "shell", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
            l2_result_target_tokens=20,
        )
    )

    part = result.view.messages[1].parts[0]
    assert part.metadata["compaction_state"] == "archived"
    assert part.metadata["lifecycle"] == "derived"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == raw_content


def test_l2_archives_large_load_skill_result_through_generic_tool_path(tmp_path: Path) -> None:
    skill_content = "Loaded skill: review\n\n# Review\n\n" + ("Check correctness.\n" * 1_000)
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("load_skill_l2", "load_skill", {"name": "review"}),
            _tool_result("load_skill_l2", "load_skill", content=skill_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    part = result.view.messages[1].parts[0]
    assert part.metadata["compaction_state"] == "archived"
    assert part.metadata["tool_name"] == "load_skill"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == skill_content
    validate_tool_call_sequence(result.view.messages)


def test_l2_skips_pinned_derived_result(tmp_path: Path) -> None:
    raw_content = "pinned result\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pinned_l2", "shell", {"command": "long-command"}),
            _tool_result(
                "pinned_l2",
                "shell",
                content=raw_content,
                metadata={"compaction_state": "pinned"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert result.view.messages[1].parts[0].content == raw_content
    assert result.event.noop is True
    assert not (tmp_path / "archives").exists()


def test_l2_never_routes_text_even_when_force_flag_is_set(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "diff --git a/lanscoder/app.py b/lanscoder/app.py",
            "--- a/lanscoder/app.py",
            "+++ b/lanscoder/app.py",
            "@@ -1,4 +1,4 @@",
            *[f" context {line}" for line in range(1, 40)],
            "-old line",
            "+new line",
            *[f" more context {line}" for line in range(40, 80)],
        ]
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_diff_hot",
                content=content,
                created_turn=10,
                metadata={"tool_name": "git_diff"},
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2",),
            force_route_current_text=True,
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.content == content
    assert part.metadata.get("compaction_state") is None
    assert result.event.noop is True


def test_pipeline_stops_after_budget_target_is_met(tmp_path: Path) -> None:
    view, _ = _derived_tool_result_view(content="x" * 40_000)

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1000,
            current_turn=10,
        )
    )

    assert result.event.stopped_at in {"l1", "l2", "not_reached"}
    assert result.event.levels_attempted[0] == "l1"
    assert result.view.messages[1].parts[0].metadata.get("compaction_state") != "archived"


def test_pipeline_does_nothing_when_already_within_budget(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, large_tool_result_tokens=20).compact(
        _request(
            view=view,
            target_tokens=10_000,
            current_turn=10,
        )
    )

    assert result.event.noop is True
    assert result.event.levels_attempted == []
    assert result.event.stopped_at == "already_within_budget"
    assert result.view.messages[0].parts[0].content == "旧任务内容" * 80
    assert result.view.messages[1].parts[0].content == "large tool output\n" * 200
    assert not (tmp_path / "archives").exists()


def test_already_within_budget_noop_is_deduped(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_current", content="short")],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = _request(
        view=view,
        target_tokens=10_000,
        current_turn=10,
    )

    first = pipeline.compact(request)
    second = pipeline.compact(request)

    assert first.event.noop is True
    assert first.event.deduped is False
    assert second.event.noop is True
    assert second.event.deduped is True


def test_noop_compaction_is_recorded_and_deduped(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_current", content="short")],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = _request(
        view=view,
        target_tokens=1,
        current_turn=10,
    )

    first = pipeline.compact(request)
    second = pipeline.compact(request)

    assert first.event.noop is True
    assert first.event.input_fingerprint == second.event.input_fingerprint
    assert second.event.deduped is True


def test_pipeline_does_not_replace_part_when_compaction_would_increase_tokens(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_short_cold",
                content="短",
                created_turn=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert result.view.messages[0].parts[0].content == "短"
    assert result.event.noop is True


def test_l2_keeps_fresh_large_view_and_structured_tool_call_byte_identical(tmp_path: Path) -> None:
    call = _tool_call("view_fresh", "view", {"path": "lanscoder/context.py", "offset": 0, "limit": 500})
    raw_call = call.parts[0].to_dict()
    raw_content = "source line\n" * 1_000
    result = _tool_result(
        "view_fresh",
        "view",
        content=raw_content,
        data={
            "path": "lanscoder/context.py",
            "start_line": 1,
            "end_line": 500,
            "total_lines": 2_000,
            "truncated": True,
        },
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=SessionView(session_id="sess_test", messages=[call, result]),
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert compacted.view.messages[0].parts[0].to_dict() == raw_call
    assert compacted.view.messages[1].parts[0].content == raw_content
    assert compacted.view.messages[1].parts[0].metadata["data"] == result.parts[0].metadata["data"]
    assert compacted.event.lifecycle_counts["fresh"] == 1
    assert compacted.event.noop is True
    assert not (tmp_path / "archives").exists()


def test_l2_archives_stale_view_and_keeps_raw_backing(tmp_path: Path) -> None:
    raw_content = "before edit source\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_stale", "view", {"path": "lanscoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "view_stale",
                "view",
                content=raw_content,
                data={
                    "path": "lanscoder/context.py",
                    "start_line": 1,
                    "end_line": 500,
                    "total_lines": 2_000,
                    "truncated": True,
                },
            ),
            _tool_call("edit_stale", "edit", {"path": "lanscoder/context.py"}),
            _tool_result("edit_stale", "edit", content="updated", data={"path": "lanscoder/context.py"}),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    archived = compacted.view.messages[1].parts[0]
    assert archived.kind == "tool_result"
    assert archived.metadata["lifecycle"] == "stale"
    assert archived.metadata["lifecycle_reason"] == "source_mutated"
    assert "lifecycle=stale" in archived.content
    assert raw_content not in archived.content
    record, backed = ToolResultArchive(tmp_path).read("sess_test", archived.metadata["archive_id"])
    assert backed == raw_content
    assert record.archive_id == archived.metadata["archive_id"]
    assert compacted.event.lifecycle_counts["stale"] == 1


def test_l2_archives_superseded_view_after_later_covering_view(tmp_path: Path) -> None:
    first_content = "first read\n" * 1_000
    second_content = "later source of truth\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_first", "view", {"path": "lanscoder/context.py", "offset": 0, "limit": 100}),
            _tool_result(
                "view_first",
                "view",
                content=first_content,
                data={"path": "lanscoder/context.py", "start_line": 1, "end_line": 100, "total_lines": 500, "truncated": True},
            ),
            _tool_call("view_second", "view", {"path": "lanscoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "view_second",
                "view",
                content=second_content,
                data={"path": "lanscoder/context.py", "start_line": 1, "end_line": 500, "total_lines": 500, "truncated": False},
            ),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    old_read = compacted.view.messages[1].parts[0]
    new_read = compacted.view.messages[3].parts[0]
    assert old_read.metadata["lifecycle"] == "superseded"
    assert "lifecycle=superseded" in old_read.content
    assert new_read.content == second_content
    assert compacted.event.lifecycle_counts["superseded"] == 1
    assert compacted.event.lifecycle_counts["fresh"] == 1


def test_l2_archives_duplicate_derived_results_under_the_same_content_addressed_id(tmp_path: Path) -> None:
    raw_content = "derived shell output\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("duplicate_one", "shell", {"command": "pwd"}),
            _tool_result("duplicate_one", "shell", content=raw_content),
            _tool_call("duplicate_two", "shell", {"command": "pwd"}),
            _tool_result("duplicate_two", "shell", content=raw_content),
            _tool_call("duplicate_three", "shell", {"command": "pwd"}),
            _tool_result("duplicate_three", "shell", content=raw_content),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2",),
            l2_result_target_tokens=10_000,
        )
    )

    first = compacted.view.messages[1].parts[0]
    second = compacted.view.messages[3].parts[0]
    latest = compacted.view.messages[5].parts[0]
    assert first.metadata["lifecycle"] == second.metadata["lifecycle"] == "duplicate"
    assert first.metadata["archive_id"] == second.metadata["archive_id"]
    assert latest.content == raw_content
    assert len(list((tmp_path / "archives" / "sess_test").glob("*.txt"))) == 1
    assert compacted.event.lifecycle_counts["duplicate"] == 2
    assert compacted.event.lifecycle_counts["derived"] == 1


def test_l2_skips_current_turn_protected_archive_retrieval_duplicate(tmp_path: Path) -> None:
    raw_content = "retrieved archive content\n" * 1_000
    protected = {"data": {"archive_retrieval": True, "compaction_protected_until_turn": 10}}
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("retrieval_one", "retrieve_archive", {"archive_id": "ar_example"}),
            _tool_result("retrieval_one", "retrieve_archive", content=raw_content, metadata=protected),
            _tool_call("retrieval_two", "retrieve_archive", {"archive_id": "ar_example"}),
            _tool_result("retrieval_two", "retrieve_archive", content=raw_content, metadata=protected),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        _request(
            view=view,
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert compacted.view.messages[1].parts[0].content == raw_content
    assert compacted.view.messages[1].parts[0].metadata.get("compaction_state") is None
    assert compacted.event.lifecycle_counts["duplicate"] == 1
    assert not (tmp_path / "archives").exists()
