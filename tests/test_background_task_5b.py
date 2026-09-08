from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from lanscoder.agent.session import AgentSession
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.tool_execution import ToolExecutor
from lanscoder.context.store import InMemorySessionStore, JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.observability.context import get_trace_id, trace_context
from lanscoder.observability.models import TraceScope, project_trace
from lanscoder.observability.recorder import JournalTraceRecorder
from lanscoder.tools.types import make_text_result
from lanscoder.planning.reducer import TaskPlanRevisionConflict
from lanscoder.planning.service import TaskPlanService
from lanscoder.session.branch import SessionBranchContext
from lanscoder.journal.models import new_branch_id
from lanscoder.utils.cancellation import current_cancellation_token


def _create_plan(session: AgentSession) -> None:
    result = session.tool_registry.execute(
        "task_create",
        {
            "mode": "dag",
            "expected_revision": 0,
            "tasks": [
                {"id": "a", "content": "A", "status": "in_progress"},
                {"id": "b", "content": "B", "status": "in_progress"},
            ],
        },
    )
    assert result.ok


def test_task_plan_mutation_can_target_an_inactive_dispatch_branch(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_branch_plan")
    _create_plan(session)
    root_context = session.writer.branch_context
    assert root_context is not None

    branch_id = new_branch_id()
    events = store.list_events(session.session_id)
    store.append_journal_event(
        session_id=session.session_id,
        kind="session.recalled",
        branch_id=branch_id,
        data={
            "new_branch_id": branch_id,
            "parent_branch_id": root_context.branch_id,
            "base_sequence": events[-1].sequence,
        },
    )
    child_context = SessionBranchContext(session.session_id, branch_id, root_context.root_branch_id)

    mutation = TaskPlanService(store=store, writer=session.writer).update(
        expected_revision=1,
        updates=[{"id": "a", "status": "completed"}],
        branch_context=root_context,
    )

    assert mutation.plan.revision == 2
    active_plan = store.rebuild_session_view(session.session_id).task_plan
    assert active_plan is not None
    assert active_plan.revision == 1
    assert active_plan.tasks[0].status == "in_progress"
    branch_writer = SessionEventWriter(
        store=store,
        session_id=session.session_id,
        branch_context=child_context,
    )
    child_plan = TaskPlanService(store=store, writer=branch_writer).current()
    assert child_plan is not None
    assert child_plan.revision == 1


def test_task_plan_current_rejects_a_context_from_another_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_branch_context_owner")
    _create_plan(session)
    branch = session.writer.branch_context
    assert branch is not None

    foreign_context = SessionBranchContext(
        "sess_other",
        branch.branch_id,
        branch.root_branch_id,
    )

    with pytest.raises(ValueError, match="session_id"):
        TaskPlanService(store=store, writer=session.writer).current(branch_context=foreign_context)


def test_store_branch_projection_validates_root_and_replays_target_branch(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_branch_projection")
    _create_plan(session)
    branch = session.writer.branch_context
    assert branch is not None

    view = store.rebuild_session_view(session.session_id, branch_context=branch)

    assert view.task_plan is not None
    assert view.task_plan.revision == 1
    with pytest.raises(ValueError, match="root"):
        store.rebuild_session_view(
            session.session_id,
            branch_context=SessionBranchContext(
                session.session_id,
                branch.branch_id,
                "wrong-root",
            ),
        )


def test_store_branch_projection_requires_a_persisted_session_root(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    context = SessionBranchContext("sess_missing_root", "root", "root")

    with pytest.raises(ValueError, match="session.created"):
        store.rebuild_session_view("sess_missing_root", branch_context=context)


def test_task_plan_mutation_requires_a_persisted_session_root(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_missing_plan_root")
    context = SessionBranchContext("sess_missing_plan_root", "root", "root")

    with pytest.raises(ValueError, match="session.created"):
        TaskPlanService(store=store, writer=writer).create(
            mode="linear",
            expected_revision=0,
            tasks=[{"id": "work", "content": "Work"}],
            branch_context=context,
        )

    assert store.list_events("sess_missing_plan_root") == []


def test_concurrent_branch_mutations_retain_both_updates_after_retry(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_concurrent_plan")
    _create_plan(session)
    branch_context = session.writer.branch_context
    assert branch_context is not None

    def complete(task_id: str) -> None:
        service = TaskPlanService(store=store, writer=session.writer)
        expected = 1
        for _ in range(4):
            try:
                service.update(
                    expected_revision=expected,
                    updates=[{"id": task_id, "status": "completed"}],
                    branch_context=branch_context,
                )
                return
            except TaskPlanRevisionConflict as error:
                expected = error.actual
        pytest.fail(f"could not retry task {task_id}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(complete, ("a", "b")))

    plan = TaskPlanService(store=store, writer=session.writer).current()
    assert plan is not None
    assert plan.revision == 3
    assert {task.id for task in plan.tasks if task.status == "completed"} == {"a", "b"}


def test_dispatch_context_uses_target_branch_head_not_global_journal_tail(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_branch_dispatch_head")
    root_context = session.writer.branch_context
    assert root_context is not None
    base_sequence = store.list_events(session.session_id)[-1].sequence

    branch_id = new_branch_id()
    store.append_journal_event(
        session_id=session.session_id,
        kind="session.recalled",
        branch_id=branch_id,
        data={
            "new_branch_id": branch_id,
            "parent_branch_id": root_context.branch_id,
            "base_sequence": base_sequence,
        },
    )
    branch_context = SessionBranchContext(session.session_id, branch_id, root_context.root_branch_id)
    store.append_journal_event(
        session_id=session.session_id,
        kind="background.completed",
        branch_id=root_context.branch_id,
        data={"job_id": "bg_old", "status": "completed"},
    )

    executor = ToolExecutor.__new__(ToolExecutor)
    executor.session = session
    dispatch = executor._background_dispatch_context(
        branch_context=branch_context,
        observed_revision=None,
    )

    assert dispatch["branch_head_sequence_at_dispatch"] == base_sequence


def test_background_dispatch_persists_context_and_runs_with_captured_trace(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_background_trace",
        session_metadata={"project_id": "project-test"},
    )
    recorder = JournalTraceRecorder(store.journal)
    branch = session.writer.branch_context
    assert branch is not None
    scope = TraceScope(session.session_id, branch.branch_id)
    parent_trace = recorder.start_trace(scope)
    parent_observation = recorder.start_observation(
        parent_trace,
        "tool",
        scope=scope,
        data={"tool_name": "shell", "tool_call_id": "call-bg"},
    )
    seen: list[str | None] = []
    manager = BackgroundJobManager()
    try:
        with trace_context(scope, trace_id=parent_trace, observation_id=parent_observation):
            job = manager.start(
                lambda: (seen.append(get_trace_id()), make_text_result("shell", "done"))[1],
                session_id=session.session_id,
                tool_name="shell",
                dispatch_context={
                    "branch_id": branch.branch_id,
                    "branch_head_sequence_at_dispatch": store.list_events(session.session_id)[-1].sequence,
                    "project_id": "project-test",
                    "task_plan_revision": 0,
                },
                branch_context=branch,
                execution_context=None,
                trace_recorder=recorder,
                trace_scope=scope,
                parent_trace_id=parent_trace,
                parent_observation_id=parent_observation,
                session_writer=session.writer,
            )
        assert manager.wait(timeout=5)
        assert seen and seen[0] is not None and seen[0] != parent_trace
        events = store.list_events(session.session_id)
        scheduled = [event for event in events if event.kind == "background.scheduled"]
        assert scheduled and scheduled[0].data["dispatch_branch_context"]["branch_id"] == branch.branch_id
        assert any(event.kind == "background.notification.delivered" and event.data["job_id"] == job.id for event in events)
        assert any(event.kind == "trace.ended" and event.trace_id == seen[0] for event in events)
        child_started = next(event for event in events if event.kind == "trace.started" and event.trace_id == seen[0])
        link = next(event for event in events if event.kind == "trace.linked" and event.data.get("child_trace_id") == seen[0])
        assert child_started.data["parent_observation_id"] == parent_observation
        assert link.data["parent_trace_id"] == parent_trace
        assert link.data.get("parent_observation_id") in {None, parent_observation}
    finally:
        manager.shutdown()


def test_recalled_background_completion_stays_on_dispatch_branch(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_detached_background")
    _create_plan(session)
    root = session.writer.branch_context
    assert root is not None
    recorder = JournalTraceRecorder(store.journal)
    parent_scope = TraceScope(session.session_id, root.branch_id)
    parent_trace = recorder.start_trace(parent_scope)
    release = Event()

    def complete_task(job) -> str:
        TaskPlanService(store=store, writer=session.writer).update(
            expected_revision=1,
            updates=[{"id": "a", "status": "completed"}],
            branch_context=job.branch_context,
        )
        return "task completed"

    manager = BackgroundJobManager()
    try:
        job = manager.start(
            lambda: (release.wait(5), make_text_result("shell", "done"))[1],
            session_id=session.session_id,
            tool_name="shell",
            task_id="a",
            observed_revision=1,
            on_completed=complete_task,
            branch_context=root,
            dispatch_branch_context={
                "branch_id": root.branch_id,
                "branch_head_sequence_at_dispatch": store.list_events(session.session_id)[-1].sequence,
                "project_id": None,
                "task_plan_revision": 1,
            },
            trace_recorder=recorder,
            trace_scope=parent_scope,
            parent_trace_id=parent_trace,
            parent_observation_id="obs-parent",
            session_writer=session.writer,
        )
        child_branch_id = new_branch_id()
        base_sequence = store.list_events(session.session_id)[-1].sequence
        store.append_journal_event(
            session_id=session.session_id,
            kind="session.recalled",
            branch_id=child_branch_id,
            data={
                "new_branch_id": child_branch_id,
                "parent_branch_id": root.branch_id,
                "base_sequence": base_sequence,
            },
        )
        release.set()
        assert manager.wait(timeout=5) is True

        root_plan = TaskPlanService(store=store, writer=session.writer).current(branch_context=root)
        active_plan = store.rebuild_session_view(session.session_id).task_plan
        assert root_plan is not None and root_plan.revision == 2
        assert root_plan.tasks[0].status == "completed"
        assert active_plan is not None and active_plan.revision == 1
        assert active_plan.tasks[0].status == "in_progress"

        events = store.list_events(session.session_id)
        update = [event for event in events if event.kind == "task.plan.updated"][-1]
        lifecycle = next(event for event in events if event.kind == "background.completed" and event.data["job_id"] == job.id)
        delivery = next(event for event in events if event.kind == "background.notification.delivered" and event.data["job_id"] == job.id)
        assert update.branch_id == root.branch_id
        assert lifecycle.data["detached_from_active_branch"] is True
        assert delivery.data["detached_from_active_branch"] is True
        assert delivery.branch_id == root.branch_id
        assert any(event.kind == "trace.ended" and event.trace_id == job.trace_id for event in events)

        notification = manager.collect_completed()[0]
        assert notification.detached_from_active_branch is True
    finally:
        release.set()
        manager.wait(timeout=5)
        manager.shutdown()


def test_background_completion_callback_runs_before_collection() -> None:
    completed: list[str] = []
    manager = BackgroundJobManager()
    try:
        job = manager.start(
            lambda: make_text_result("shell", "done"),
            tool_name="shell",
            on_completed=lambda completed_job: completed.append(completed_job.id) or "recorded",
        )

        assert manager.wait(timeout=5) is True
        assert completed == [job.id]
        assert job.task_plan_completion == "recorded"
        assert manager.pending_completions() == [job]
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    ("outcome", "expected_kind"),
    [
        ("success", "background.completed"),
        ("failure", "background.failed"),
        ("cancel", "background.cancelled"),
    ],
)
def test_background_completion_persists_lifecycle_and_delivery_before_collection(
    tmp_path,
    outcome: str,
    expected_kind: str,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id=f"sess_lifecycle_{outcome}")
    branch = session.writer.branch_context
    assert branch is not None
    manager = BackgroundJobManager(max_workers=1)
    started = Event()
    release = Event()

    def run() -> object:
        started.set()
        if outcome == "failure":
            raise RuntimeError("worker failed")
        if outcome == "cancel":
            release.wait(5)
            token = current_cancellation_token()
            assert token is not None
            token.raise_if_cancelled()
        return make_text_result("shell", "done")

    try:
        job = manager.start(
            run,
            session_id=session.session_id,
            tool_name="shell",
            branch_context=branch,
            dispatch_branch_context={
                "branch_id": branch.branch_id,
                "branch_head_sequence_at_dispatch": store.list_events(session.session_id)[-1].sequence,
                "project_id": None,
                "task_plan_revision": None,
            },
            session_writer=session.writer,
        )
        if outcome == "cancel":
            assert started.wait(timeout=5)
            assert manager.cancel(job.id) is job
            release.set()
        assert manager.wait(timeout=5) is True

        events = store.list_events(session.session_id)
        lifecycle = next(event for event in events if event.kind == expected_kind and event.data["job_id"] == job.id)
        delivery = next(event for event in events if event.kind == "background.notification.delivered" and event.data["job_id"] == job.id)
        assert lifecycle.data["status"] == job.status
        assert delivery.data["status"] == job.status
        assert manager.pending_completions() == [job]
    finally:
        release.set()
        manager.wait(timeout=5)
        manager.shutdown()


def test_abandoning_queued_job_keeps_unknown_status_running(tmp_path) -> None:
    manager = BackgroundJobManager(max_workers=1)
    started = Event()
    release = Event()
    cleaned: list[str] = []

    try:
        manager.start(
            lambda: (started.set(), release.wait(5), make_text_result("shell", "first"))[2],
            session_id="sess_abandon_queued",
            tool_name="shell",
            dispatch_turn=1,
        )
        assert started.wait(timeout=5)
        queued = manager.start(
            lambda: make_text_result("shell", "queued"),
            session_id="sess_abandon_queued",
            tool_name="shell",
            dispatch_turn=2,
        )
        queued.worktree_cleanup = lambda: cleaned.append(queued.id)

        assert manager.abandon_since("sess_abandon_queued", min_dispatch_turn=2) == 1
        assert manager.get(queued.id) is queued
        assert queued.status == "running"
        assert queued.abandoned is True
        assert cleaned == [queued.id]
    finally:
        release.set()
        manager.wait(timeout=5)
        manager.shutdown()


def test_lifecycle_persistence_retries_only_the_failed_event() -> None:
    class FlakyWriter:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.fail_delivery = True

        def append_event(self, kind: str, _data: dict, **_kwargs) -> None:
            if kind == "background.notification.delivered" and self.fail_delivery:
                self.fail_delivery = False
                raise OSError("temporary journal failure")
            self.events.append(kind)

    writer = FlakyWriter()
    manager = BackgroundJobManager()
    try:
        job = manager.start(
            lambda: make_text_result("shell", "done"),
            session_id="sess_retry_lifecycle",
            tool_name="shell",
            session_writer=writer,
        )
        assert manager.wait(timeout=5)
        assert writer.events.count("background.completed") == 1
        assert writer.events.count("background.notification.delivered") == 0
        assert job.lifecycle_persisted is False

        manager._persist_lifecycle(job)
        assert writer.events.count("background.completed") == 1
        assert writer.events.count("background.notification.delivered") == 1
        assert job.lifecycle_persisted is True
    finally:
        manager.shutdown()


def test_trace_completion_retries_after_recorder_failure(tmp_path) -> None:
    class FlakyRecorder:
        def __init__(self) -> None:
            self.end_calls = 0

        def start_trace(self, _scope, *, data):
            return "trace-background"

        def link_trace(self, *_args, **_kwargs):
            return None

        def end_trace(self, *_args, **_kwargs):
            self.end_calls += 1
            if self.end_calls == 1:
                raise OSError("temporary recorder failure")

    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_retry_trace")
    branch = session.writer.branch_context
    assert branch is not None
    recorder = FlakyRecorder()
    manager = BackgroundJobManager()
    try:
        job = manager.start(
            lambda: make_text_result("shell", "done"),
            session_id=session.session_id,
            tool_name="shell",
            branch_context=branch,
            trace_scope=TraceScope(session.session_id, branch.branch_id),
            trace_recorder=recorder,
        )
        assert manager.wait(timeout=5)
        assert recorder.end_calls == 1
        assert job.trace_completed is False

        manager._persist_trace_completion(job)
        assert recorder.end_calls == 2
        assert job.trace_completed is True
    finally:
        manager.shutdown()


def test_writer_rejects_foreign_explicit_branch_context(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_branch_owner")
    branch = session.writer.branch_context
    assert branch is not None
    foreign = SessionBranchContext("sess_other", branch.branch_id, branch.root_branch_id)

    with pytest.raises(ValueError, match="session_id"):
        session.writer.append_event(
            "background_lifecycle",
            {"job_id": "bg_foreign", "status": "running"},
            branch_context=foreign,
            allow_inactive_branch=True,
        )


def test_writer_rejects_foreign_explicit_branch_context_for_in_memory_store() -> None:
    store = InMemorySessionStore()
    writer = SessionEventWriter(store=store, session_id="sess_branch_owner")
    foreign = SessionBranchContext("sess_other", "branch", "root")

    with pytest.raises(ValueError, match="session_id"):
        writer.append_event(
            "background_lifecycle",
            {"job_id": "bg_foreign", "status": "running"},
            branch_context=foreign,
            allow_inactive_branch=True,
        )

    assert store.list_events("sess_branch_owner") == []
    assert store.list_events("sess_other") == []


def test_journal_trace_completion_failure_remains_retryable(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_retry_journal_trace")
    branch = session.writer.branch_context
    assert branch is not None
    recorder = JournalTraceRecorder(store.journal)

    original_append = store.journal.append
    failed = True

    def append_with_one_trace_failure(kind, *args, **kwargs):
        nonlocal failed
        if kind == "trace.ended" and failed:
            failed = False
            raise OSError("temporary journal failure")
        return original_append(kind, *args, **kwargs)

    manager = BackgroundJobManager()
    try:
        release = Event()
        started = Event()

        def run() -> object:
            started.set()
            release.wait(5)
            return make_text_result("shell", "done")

        job = manager.start(
            run,
            session_id=session.session_id,
            tool_name="shell",
            branch_context=branch,
            trace_scope=TraceScope(session.session_id, branch.branch_id),
            trace_recorder=recorder,
        )
        assert started.wait(timeout=5)
        store.journal.append = append_with_one_trace_failure
        release.set()
        assert manager.wait(timeout=5)
        assert job.trace_completed is False
        assert job.trace_id is not None
        assert not any(event.kind == "trace.ended" and event.trace_id == job.trace_id for event in store.list_events(session.session_id))

        store.journal.append = original_append
        manager._persist_trace_completion(job)
        assert job.trace_completed is True
        assert any(event.kind == "trace.ended" and event.trace_id == job.trace_id for event in store.list_events(session.session_id))
    finally:
        store.journal.append = original_append
        manager.shutdown()


def test_unclosed_background_trace_projects_as_incomplete_after_restart(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_incomplete_background")
    branch = session.writer.branch_context
    assert branch is not None
    recorder = JournalTraceRecorder(store.journal)
    trace_id = recorder.start_trace(
        TraceScope(session.session_id, branch.branch_id),
        data={"operation": "background", "job_id": "bg_crashed"},
    )

    restarted_record = project_trace(store.list_events(session.session_id), trace_id)

    assert restarted_record.status.value == "running"
    assert restarted_record.incomplete is True
    assert not any(event.kind == "trace.ended" and event.trace_id == trace_id for event in store.list_events(session.session_id))
