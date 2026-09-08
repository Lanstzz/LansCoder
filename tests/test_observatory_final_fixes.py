from __future__ import annotations

from pathlib import Path
import inspect

import pytest

import lanscoder.cli as cli
from lanscoder.context.archive import ToolResultArchive
from lanscoder.context.compaction import CompactionPipeline
from lanscoder.context.manager import ContextWindowManager
from lanscoder.context.models import MessagePart
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.core.session import create_agent_session
from lanscoder.input.attachments import attach_path, prepare_attachments_for_session, resolve_paste_attachments
from lanscoder.observability.models import TraceScope, project_trace
from lanscoder.observability.recorder import JournalTraceRecorder
from lanscoder.observability.web.api import ObservatoryQueryService
from lanscoder.agent.subagent_engine import SubagentEngine
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.fork import ForkSessionService, _rewrite_fork_data
from lanscoder.storage import LansCoderPaths


def test_bootstrap_persists_primary_identity_in_session_created(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)

    session = SessionBootstrap(store=store, project_root=tmp_path, paths=paths).create(session_id="sess_primary")

    created = store.list_events(session.session_id)[0]
    assert created.kind == "session.created"
    assert created.data["project_id"] == paths.project_id
    assert created.data["project_root"] == str(paths.project_root)
    assert created.data["kind"] == "primary"


def test_explicit_paths_are_used_for_attachment_archive_and_clipboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")

    prepared = prepare_attachments_for_session([attach_path(source)], paths=paths, session_id="sess_a")
    assert (paths.storage_root / prepared[0].relative_path).is_file()
    assert not (tmp_path / "runtime" / "attachments").exists()

    part = MessagePart(id="part", message_id="message", kind="tool_result", content="archived")
    record = ToolResultArchive(paths).store_original("sess_a", part)
    assert (paths.archives / "sess_a" / f"{record.archive_id}.txt").is_file()

    monkeypatch.setattr("lanscoder.input.attachments.read_clipboard_image_bytes", lambda: b"\x89PNG\r\n\x1a\n")
    attachments = resolve_paste_attachments(None, paths=paths)
    assert attachments
    assert attachments[0].path.parent == paths.clipboard_tmp


def test_compaction_and_archive_require_canonical_paths(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)

    manager = ContextWindowManager(store=store)

    assert isinstance(manager.pipeline, CompactionPipeline)
    assert manager.pipeline.paths is not None
    assert manager.pipeline.paths.storage_root == paths.storage_root
    with pytest.raises(TypeError, match="LansCoderPaths"):
        ToolResultArchive(paths.storage_root)


def test_recorder_payload_failure_keeps_only_incomplete_descriptor(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)

    class FailingPayloads:
        def put_json(self, value, *, media_type="application/json"):
            raise OSError("payload unavailable")

    writer = SessionEventWriter(store=store, session_id="sess_record")
    writer.append_session_created()
    recorder = JournalTraceRecorder(store.journal, FailingPayloads(), inline_payload_limit=8)
    scope = TraceScope("sess_record", writer.branch_context.branch_id)
    trace_id = recorder.start_trace(scope)
    assert recorder.end_trace(trace_id, final_output={"raw": "x" * 100}) is True

    ended = next(event for event in store.list_events("sess_record") if event.kind == "trace.ended")
    assert "final_output" not in ended.data
    assert ended.data["evidence_incomplete"] is True


def test_root_trace_contains_git_snapshot_metadata_and_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lanscoder.core import runtime
    from lanscoder.observability.git import GitSnapshot

    monkeypatch.setattr(runtime, "snapshot_git", lambda _: GitSnapshot("head", "main", True))
    handle = create_agent_session(
        provider=_Provider(),
        project_root=tmp_path,
        storage_root=tmp_path / "runtime",
        tools=[],
        compaction_strategy="no_compact",
    )
    handle.runner.run_user_turn("hello")
    started = next(event for event in handle.session.store.list_events(handle.session.session_id) if event.kind == "trace.started")
    assert started.data["project_id"]
    assert started.data["git_head"] == "head"
    record = project_trace(handle.session.store.list_events(handle.session.session_id), started.trace_id)
    assert record.metadata["git_head"] == "head"


def test_replay_projects_persisted_background_notification(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_replay")
    writer.append_session_created(project_id=paths.project_id, kind="primary")
    writer.append_background_notification(content="finished", job_id="job_1", tool_name="shell", status="completed")
    store.append_journal_event(
        session_id="sess_replay",
        kind="background.completed",
        data={"job_id": "job_1", "background_trace_id": "trc_bg", "status": "completed"},
        branch_id=writer.branch_context.branch_id,
    )

    replay = ObservatoryQueryService(paths).replay_session("sess_replay")
    notification = next(item for item in replay["items"] if item["role"] == "notification")
    assert notification["status"] == "completed"
    assert notification["linked_trace_ids"] == ["trc_bg"]


def test_replay_notification_links_only_its_background_job_trace(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_replay_jobs")
    writer.append_session_created(project_id=paths.project_id, kind="primary")
    writer.append_background_notification(content="first", job_id="job_1", tool_name="shell", status="completed")
    writer.append_background_notification(content="second", job_id="job_2", tool_name="shell", status="failed")
    for job_id, trace_id, status in (("job_1", "trc_first", "completed"), ("job_2", "trc_second", "failed")):
        store.append_journal_event(
            session_id="sess_replay_jobs",
            kind="background.scheduled",
            data={"job_id": job_id, "parent_trace_id": "trc_parent"},
            branch_id=writer.branch_context.branch_id,
        )
        store.append_journal_event(
            session_id="sess_replay_jobs",
            kind=f"background.{status}",
            data={"job_id": job_id, "background_trace_id": trace_id, "status": status},
            branch_id=writer.branch_context.branch_id,
        )
        store.append_journal_event(
            session_id="sess_replay_jobs",
            kind="trace.linked",
            data={
                "job_id": job_id,
                "parent_trace_id": "trc_parent",
                "child_trace_id": f"{trace_id}_linked",
                "relation": "background",
            },
            branch_id=writer.branch_context.branch_id,
        )

    items = ObservatoryQueryService(paths).replay_session("sess_replay_jobs")["items"]

    assert {item["content"]: item["linked_trace_ids"] for item in items if item["role"] == "notification"} == {
        "first": ["trc_first", "trc_first_linked", "trc_parent"],
        "second": ["trc_parent", "trc_second", "trc_second_linked"],
    }


def test_observe_storage_root_is_unambiguous_before_or_after_subcommand(tmp_path: Path) -> None:
    root = str(tmp_path / "runtime")
    assert build_storage_root(["--storage-root", root, "observe"]) == Path(root)
    assert build_storage_root(["observe", "--storage-root", root]) == Path(root)


def test_observe_rejects_conflicting_parent_and_subcommand_storage_roots(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "--storage-root",
                str(tmp_path / "parent"),
                "observe",
                "--storage-root",
                str(tmp_path / "child"),
            ]
        )


def test_subagent_engine_has_no_direct_low_level_child_constructor() -> None:
    assert "AgentSession.create" not in inspect.getsource(SubagentEngine)


def build_storage_root(argv: list[str]) -> Path:
    args = cli.build_parser().parse_args(argv)
    storage_root = args.observe_storage_root or args.storage_root
    return Path(storage_root) if storage_root is not None else Path("~/.lanscoder")


def test_fork_remaps_only_identified_id_fields() -> None:
    value = {
        "message_id": "msg_old",
        "content": "msg_old trace_old call_old",
        "arguments": {"text": "msg_old", "tool_call_id": "call_old"},
    }
    rewritten = _rewrite_fork_data(
        value,
        source_session_id="sess_old",
        forked_session_id="sess_new",
        root_branch_id="branch_new",
        id_map={"msg_old": "msg_new", "trace_old": "trace_new", "call_old": "call_new"},
        event_kind="message.appended",
    )
    assert rewritten["message_id"] == "msg_new"
    assert rewritten["content"] == value["content"]
    assert rewritten["arguments"]["text"] == "msg_old"
    assert rewritten["arguments"]["tool_call_id"] == "call_old"

    trace = _rewrite_fork_data(
        {"parent_trace_id": "trace_old", "metadata": {"trace_id": "trace_old"}},
        source_session_id="sess_old",
        forked_session_id="sess_new",
        root_branch_id="branch_new",
        id_map={"trace_old": "trace_new"},
        event_kind="trace.started",
    )
    assert trace["parent_trace_id"] == "trace_new"
    assert trace["metadata"] == {"trace_id": "trace_old"}


def test_fork_remaps_checkpoint_id_only_for_checkpoint_created_events() -> None:
    checkpoint = {
        "id": "ckpt_source",
        "content": "ckpt_source is user content",
        "arguments": {"text": "ckpt_source", "tool_call_id": "call_source"},
    }
    remapped_checkpoint = _rewrite_fork_data(
        checkpoint,
        source_session_id="sess_old",
        forked_session_id="sess_new",
        root_branch_id="branch_new",
        id_map={"ckpt_source": "ckpt_new", "call_source": "call_new"},
        event_kind="checkpoint.created",
    )
    remapped_message = _rewrite_fork_data(
        checkpoint,
        source_session_id="sess_old",
        forked_session_id="sess_new",
        root_branch_id="branch_new",
        id_map={"ckpt_source": "ckpt_new", "call_source": "call_new"},
        event_kind="message.appended",
    )

    assert remapped_checkpoint["id"] == "ckpt_new"
    assert remapped_checkpoint["content"] == checkpoint["content"]
    assert remapped_checkpoint["arguments"] == checkpoint["arguments"]
    assert remapped_message == checkpoint


def test_fork_preserves_opaque_message_and_pending_payload_ids(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_source")
    writer.append_session_created(project_id=paths.project_id, kind="primary")
    branch_id = writer.branch_context.branch_id
    opaque_metadata = {
        "tool_call_id": "call_source",
        "data": {"tool_call_id": "call_source", "trace_id": "trc_source"},
    }
    opaque_arguments = {"tool_call_id": "call_source", "trace_id": "trc_source"}
    store.append_journal_event(
        session_id="sess_source",
        kind="message.appended",
        branch_id=branch_id,
        data={
            "role": "assistant",
            "message_id": "msg_call_source",
            "metadata": opaque_metadata,
            "parts": [
                {
                    "id": "part_call_source",
                    "message_id": "msg_call_source",
                    "kind": "tool_call",
                    "content": "",
                    "metadata": {
                        "tool_call_id": "call_source",
                        "tool_name": "echo",
                        "arguments": opaque_arguments,
                    },
                }
            ],
        },
    )
    store.append_journal_event(
        session_id="sess_source",
        kind="trace.paused",
        branch_id=branch_id,
        data={
            "pending": {
                "tool_call_id": "call_source",
                "arguments": opaque_arguments,
            },
            "metadata": opaque_metadata,
        },
    )

    result = ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_source")
    events = store.list_events(result.session.session_id)
    message = next(event for event in events if event.kind == "message.appended")
    paused = next(event for event in events if event.kind == "trace.paused")

    tool_call_metadata = message.data["parts"][0]["metadata"]
    assert tool_call_metadata["tool_call_id"] != "call_source"
    assert tool_call_metadata["arguments"] == opaque_arguments
    assert message.data["metadata"] == opaque_metadata
    assert paused.data["pending"]["tool_call_id"] == tool_call_metadata["tool_call_id"]
    assert paused.data["pending"]["arguments"] == opaque_arguments
    assert paused.data["metadata"] == opaque_metadata


def test_fork_remaps_trace_linkage_without_copying_source_trace_ids(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_source")
    writer.append_session_created(project_id=paths.project_id, kind="primary")
    branch_id = writer.branch_context.branch_id
    store.journal.append(
        "trace.started",
        {},
        session_id="sess_source",
        trace_id="trc_parent_source",
        branch_id=branch_id,
    )
    store.journal.append(
        "trace.started",
        {"parent_trace_id": "trc_parent_source"},
        session_id="sess_source",
        trace_id="trc_child_source",
        observation_id="obs_child_source",
        parent_observation_id="obs_parent_source",
        branch_id=branch_id,
    )
    store.journal.append(
        "trace.linked",
        {
            "parent_trace_id": "trc_parent_source",
            "child_trace_id": "trc_child_source",
            "parent_session_id": "sess_source",
            "child_session_id": "sess_source",
        },
        session_id="sess_source",
        trace_id="trc_child_source",
        branch_id=branch_id,
    )

    result = ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_source")
    events = store.list_events(result.session.session_id)
    forked_parent = next(event for event in events if event.kind == "trace.started" and event.data.get("parent_trace_id") is None)
    forked_child = next(event for event in events if event.kind == "trace.started" and event.data.get("parent_trace_id") is not None)
    linked = next(event for event in events if event.kind == "trace.linked")

    assert forked_parent.trace_id not in {None, "trc_parent_source"}
    assert forked_child.trace_id not in {None, "trc_child_source"}
    assert forked_child.observation_id not in {None, "obs_child_source"}
    assert forked_child.parent_observation_id not in {None, "obs_parent_source"}
    assert forked_child.data["parent_trace_id"] == forked_parent.trace_id
    assert linked.trace_id == forked_child.trace_id
    assert linked.data == {
        "parent_trace_id": forked_parent.trace_id,
        "child_trace_id": forked_child.trace_id,
        "parent_session_id": result.session.session_id,
        "child_session_id": result.session.session_id,
    }


def test_fork_excludes_primary_side_link_to_delegated_child_trace(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "runtime", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    parent_writer = SessionEventWriter(store=store, session_id="sess_source")
    parent_writer.append_session_created(project_id=paths.project_id, kind="primary")
    child_writer = SessionEventWriter(store=store, session_id="sess_delegated_child")
    child_writer.append_session_created(
        project_id=paths.project_id,
        kind="subagent",
        parent_session_id="sess_source",
    )
    recorder = JournalTraceRecorder(store.journal)
    parent_trace_id = recorder.start_trace(TraceScope("sess_source", parent_writer.branch_context.branch_id))
    child_trace_id = recorder.start_trace(
        TraceScope(
            "sess_delegated_child",
            child_writer.branch_context.branch_id,
            parent_trace_id=parent_trace_id,
        )
    )
    recorder.link_trace(
        parent_trace_id,
        child_trace_id,
        relation="child",
        data={
            "parent_session_id": "sess_source",
            "child_session_id": "sess_delegated_child",
        },
    )

    source_link = next(event for event in store.list_events("sess_source") if event.kind == "trace.linked")
    assert source_link.data["child_trace_id"] == child_trace_id
    assert source_link.data["child_session_id"] == "sess_delegated_child"

    result = ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_source")
    forked_events = store.list_events(result.session.session_id)

    assert not any(event.kind == "trace.linked" for event in forked_events)
    assert all(child_trace_id not in str(event.to_dict()) for event in forked_events)
    assert all("sess_delegated_child" not in str(event.to_dict()) for event in forked_events)


class _Provider:
    name = "fake"
    model = "fake"

    def complete(self, request):
        from lanscoder.providers.types import ChatResponse

        return ChatResponse(provider="fake", model="fake", content="ok")
