from __future__ import annotations

import threading
import time
from html import escape
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from lanscoder.providers.types import ToolDefinition
from lanscoder.utils.cancellation import (
    AgentCancelledError,
    CancellationToken,
    cancellation_context,
)
from lanscoder.tools.types import ToolResult, make_text_result


RUN_IN_BACKGROUND_ARG = "run_in_background"
BACKGROUND_LABEL_ARG = "background_label"
BACKGROUND_TASK_ID_ARG = "task_id"
BACKGROUND_CONTROL_ARGS = frozenset({RUN_IN_BACKGROUND_ARG, BACKGROUND_LABEL_ARG, BACKGROUND_TASK_ID_ARG})

DEFAULT_BACKGROUND_TOOL_NAMES = frozenset(
    {
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "read_multi",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
        "shell",
        "python_exec",
        "fetch",
        "web_search",
        "delegate",
    }
)

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_SUMMARY_PREVIEW_LIMIT = 2000


def with_background_controls(definition: ToolDefinition) -> ToolDefinition:

    parameters: dict[str, Any] = deepcopy(definition.parameters) if definition.parameters else {}
    parameters.setdefault("type", "object")
    properties = parameters.setdefault("properties", {})
    if not isinstance(properties, dict):
        return definition
    properties[RUN_IN_BACKGROUND_ARG] = {
        "type": "boolean",
        "description": (
            "Set true to run this tool asynchronously in the background. You immediately "
            "get a job id placeholder; the real result arrives later as a "
            "<task_notification>. Use for slow, independent work that should not block "
            "the next step."
        ),
    }
    properties[BACKGROUND_LABEL_ARG] = {
        "type": "string",
        "description": "Optional short label to recognise this background job in status/notifications.",
    }
    properties[BACKGROUND_TASK_ID_ARG] = {
        "type": "string",
        "description": (
            "Optional TaskPlan task ID to associate with this background job. The task must already " "exist in the current plan; successful completion advances that same task when it remains active."
        ),
    }
    return ToolDefinition(
        name=definition.name,
        description=definition.description,
        parameters=parameters,
    )


def strip_background_controls(arguments: Any) -> tuple[dict[str, Any], bool, str | None, str | None]:

    if not isinstance(arguments, dict):
        return {} if arguments is None else arguments, False, None, None
    clean = {key: value for key, value in arguments.items() if key not in BACKGROUND_CONTROL_ARGS}
    run_in_background = bool(arguments.get(RUN_IN_BACKGROUND_ARG))
    raw_label = arguments.get(BACKGROUND_LABEL_ARG)
    raw_task_id = arguments.get(BACKGROUND_TASK_ID_ARG)
    label = str(raw_label).strip() if isinstance(raw_label, str) and raw_label.strip() else None
    task_id = str(raw_task_id).strip() if isinstance(raw_task_id, str) and raw_task_id.strip() else None
    return clean, run_in_background, label, task_id


def has_background_control_fields(arguments: Any) -> bool:

    return isinstance(arguments, dict) and any(key in arguments for key in BACKGROUND_CONTROL_ARGS)


@dataclass(slots=True)
class BackgroundNotification:

    job_id: str
    tool_name: str
    status: str
    summary: str
    ok: bool
    session_id: str | None = None
    label: str | None = None
    task_id: str | None = None
    observed_revision: int | None = None
    task_plan_completion: str | None = None
    elapsed_seconds: float | None = None
    provider_calls: int | None = None
    total_tokens: int | None = None
    kind: str = "tool"


@dataclass(slots=True)
class BackgroundJob:

    id: str
    tool_name: str
    session_id: str | None = None
    label: str | None = None
    task_id: str | None = None
    observed_revision: int | None = None
    dispatch_turn: int | None = None
    status: str = STATUS_RUNNING
    result: ToolResult | None = None
    error: str | None = None
    cancel_requested: bool = False
    abandoned: bool = False
    created_at: float = 0.0
    token: CancellationToken = field(default_factory=CancellationToken)
    on_completed: Callable[["BackgroundJob"], str | None] | None = field(default=None, repr=False)
    task_plan_completion: str | None = None
    worktree_cleanup: Callable[[], None] | None = field(default=None, repr=False)
    progress: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:

        summary = _summarize(self) if self.status != STATUS_RUNNING else None
        return {
            "job_id": self.id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "label": self.label,
            "task_id": self.task_id,
            "observed_revision": self.observed_revision,
            "status": self.status,
            "ok": None if self.result is None else self.status == STATUS_COMPLETED,
            "error": self.error,
            "summary": summary,
            "cancel_requested": self.cancel_requested,
        }


_current_job_id: threading.local = threading.local()


def current_job_id() -> str | None:
    return getattr(_current_job_id, "value", None)


class BackgroundJobManager:

    def __init__(self, *, max_jobs: int = 8, max_workers: int = 4, clock: Callable[[], float] | None = None) -> None:
        self.max_jobs = max_jobs
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fc-bg")
        self._lock = threading.Lock()
        self._jobs: dict[str, BackgroundJob] = {}
        self._futures: dict[str, Future] = {}
        self._completed: deque[BackgroundJob] = deque()
        self._counter = 0
        self._clock = clock or time.monotonic
        self._on_job_completed: Callable[[BackgroundJob], None] | None = None

    def set_on_job_completed(self, cb: Callable[[BackgroundJob], None]) -> None:
        self._on_job_completed = cb

    def start(
        self,
        func: Callable[[], ToolResult],
        *,
        session_id: str | None = None,
        tool_name: str,
        label: str | None = None,
        task_id: str | None = None,
        observed_revision: int | None = None,
        dispatch_turn: int | None = None,
        on_completed: Callable[[BackgroundJob], str | None] | None = None,
    ) -> BackgroundJob:

        with self._lock:
            active = sum(1 for job in self._jobs.values() if job.status == STATUS_RUNNING)
            if active >= self.max_jobs:
                raise BackgroundCapacityError(self.max_jobs)
            self._counter += 1
            job_id = f"bg_{self._counter:04d}"
            job = BackgroundJob(
                id=job_id,
                session_id=session_id,
                tool_name=tool_name,
                label=label,
                task_id=task_id,
                observed_revision=observed_revision,
                dispatch_turn=dispatch_turn,
                created_at=self._clock(),
                on_completed=on_completed,
            )
            self._jobs[job_id] = job
            future = self._executor.submit(self._run, job, func)
            self._futures[job_id] = future
        return job

    def _run(self, job: BackgroundJob, func: Callable[[], ToolResult]) -> None:
        _current_job_id.value = job.id
        try:
            with cancellation_context(job.token):
                result = func()
        except AgentCancelledError:
            self._finish(job, result=None, error=None)
            return
        except Exception as exc:  # noqa: BLE001 - 后台失败必须转成 notification，而不是吞掉
            self._finish(job, result=None, error=f"后台任务执行失败：{exc}")
            return
        self._finish(job, result=result, error=None)

    def _finish(self, job: BackgroundJob, *, result: ToolResult | None, error: str | None) -> None:
        abandoned_cleanup: Callable[[], None] | None = None
        with self._lock:
            if job.status == STATUS_CANCELLED:
                job.result = result
                self._futures.pop(job.id, None)
                return
            if job.abandoned:
                self._futures.pop(job.id, None)
                abandoned_cleanup = job.worktree_cleanup
            else:
                job.result = result
                job.error = error
                if job.cancel_requested:
                    job.status = STATUS_CANCELLED
                elif error is not None:
                    job.status = STATUS_FAILED
                elif result is not None and not result.ok:
                    job.status = STATUS_FAILED
                else:
                    job.status = STATUS_COMPLETED
                self._futures.pop(job.id, None)
                self._completed.append(job)

        if abandoned_cleanup is not None:
            self._invoke_worktree_cleanup(abandoned_cleanup)
            return

        cb = self._on_job_completed
        if cb is not None:
            cb(job)

    @staticmethod
    def _invoke_worktree_cleanup(cleanup: Callable[[], None]) -> None:

        try:
            cleanup()
        except Exception:  # noqa: BLE001 - orphan cleanup is opportunistic.
            pass

    def _notification_for(self, job: BackgroundJob) -> BackgroundNotification:
        summary = _summarize(job)
        ok = job.status == STATUS_COMPLETED
        progress = job.progress or {}
        total_tokens = progress.get("total_tokens")
        provider_calls = progress.get("provider_calls")
        has_usage = total_tokens is not None or provider_calls is not None
        return BackgroundNotification(
            job_id=job.id,
            session_id=job.session_id,
            tool_name=job.tool_name,
            status=job.status,
            summary=summary,
            ok=ok,
            label=job.label,
            task_id=job.task_id,
            observed_revision=job.observed_revision,
            task_plan_completion=job.task_plan_completion,
            elapsed_seconds=(self._clock() - job.created_at) if has_usage else None,
            provider_calls=provider_calls if has_usage else None,
            total_tokens=total_tokens if has_usage else None,
        )

    def collect_completed(self, *, session_id: str | None = None) -> list[BackgroundNotification]:

        with self._lock:
            jobs: list[BackgroundJob] = []
            remaining: deque[BackgroundJob] = deque()
            for job in self._completed:
                if session_id is None or job.session_id == session_id:
                    jobs.append(job)
                else:
                    remaining.append(job)
            self._completed = remaining
        notifications: list[BackgroundNotification] = []
        for job in jobs:
            self._finalize_task_plan_completion(job)
            notifications.append(self._notification_for(job))
        return notifications

    def _finalize_task_plan_completion(self, job: BackgroundJob) -> None:
        if job.status != STATUS_COMPLETED or job.on_completed is None:
            return
        try:
            job.task_plan_completion = job.on_completed(job)
        except Exception as exc:  # noqa: BLE001 - report persistence failures truthfully
            job.status = STATUS_FAILED
            job.error = f"Failed to record TaskPlan completion: {exc}"
            job.task_plan_completion = "TaskPlan completion failed; task state was not confirmed."

    def get(self, job_id: str, *, session_id: str | None = None) -> BackgroundJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or (session_id is not None and job.session_id != session_id):
                return None
            return job

    def list(self, *, session_id: str | None = None) -> list[BackgroundJob]:
        with self._lock:
            return [job for job in self._jobs.values() if session_id is None or job.session_id == session_id]

    def active_jobs(self) -> list[BackgroundJob]:
        with self._lock:
            return [job for job in self._jobs.values() if job.status == STATUS_RUNNING]

    def pending_completions(self, *, session_id: str | None = None) -> list[BackgroundJob]:

        with self._lock:
            return [job for job in self._completed if session_id is None or job.session_id == session_id]

    def cancel(self, job_id: str, *, session_id: str | None = None) -> BackgroundJob | None:

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or (session_id is not None and job.session_id != session_id):
                return None
            if job.status != STATUS_RUNNING:
                return job
            future = self._futures.get(job_id)
            if future is not None and future.cancel():
                job.status = STATUS_CANCELLED
                self._futures.pop(job_id, None)
                self._completed.append(job)
            else:
                job.cancel_requested = True
                job.token.cancel()
            return job

    def abandon_since(self, session_id: str, *, min_dispatch_turn: int) -> int:

        cleanups: list[Callable[[], None]] = []
        with self._lock:
            remaining: deque[BackgroundJob] = deque()
            abandoned = 0
            for job in self._completed:
                if job.session_id == session_id and job.dispatch_turn is not None and job.dispatch_turn >= min_dispatch_turn:
                    abandoned += 1
                    if job.worktree_cleanup is not None:
                        cleanups.append(job.worktree_cleanup)
                else:
                    remaining.append(job)
            self._completed = remaining

            for job in list(self._jobs.values()):
                if job.session_id != session_id or job.status != STATUS_RUNNING:
                    continue
                if job.dispatch_turn is None or job.dispatch_turn < min_dispatch_turn:
                    continue
                job.abandoned = True
                job.cancel_requested = True
                job.token.cancel()
                future = self._futures.get(job.id)
                if future is not None and future.cancel():
                    job.status = STATUS_CANCELLED
                    self._futures.pop(job.id, None)
                abandoned += 1

        for cleanup in cleanups:
            self._invoke_worktree_cleanup(cleanup)
        return abandoned

    def wait(self, timeout: float | None = None) -> bool:

        with self._lock:
            futures = list(self._futures.values())
        if not futures:
            return True
        _, not_done = futures_wait(futures, timeout=timeout)
        return not not_done

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class BackgroundCapacityError(RuntimeError):

    def __init__(self, max_jobs: int) -> None:
        super().__init__(f"后台任务已达上限（{max_jobs}）。请等待现有任务完成或取消后再试。")
        self.max_jobs = max_jobs


def make_background_placeholder_result(job: BackgroundJob) -> ToolResult:

    label_hint = f"（{job.label}）" if job.label else ""
    content = (
        f"Background job {job.id} started for {job.tool_name}{label_hint}.\n"
        f"Result will be delivered as <task_notification> when complete.\n"
        f"Continue dispatching any other independent sub-tasks now; do not wait "
        f"for this one to finish.\n"
        f'Use background_status or background_cancel with job_id="{job.id}" if needed.'
    )
    return make_text_result(
        job.tool_name,
        content,
        background_job_id=job.id,
        tool_name=job.tool_name,
        status=STATUS_RUNNING,
        notification_pending=True,
        task_id=job.task_id,
        observed_revision=job.observed_revision,
    )


def render_task_notification(notification: BackgroundNotification) -> str:

    label = escape(notification.label, quote=False) if notification.label else None
    lines = [
        "<task_notification>",
        f"  <job_id>{escape(notification.job_id, quote=False)}</job_id>",
        f"  <kind>{escape(notification.kind, quote=False)}</kind>",
        f"  <tool_name>{escape(notification.tool_name, quote=False)}</tool_name>",
    ]
    if label:
        lines.append(f"  <label>{label}</label>")
    if notification.task_id:
        lines.append(f"  <task_id>{escape(notification.task_id, quote=False)}</task_id>")
    if notification.observed_revision is not None:
        lines.append(f"  <observed_revision>{notification.observed_revision}</observed_revision>")
    if notification.task_plan_completion:
        lines.append("  <task_plan_completion>" f"{escape(notification.task_plan_completion, quote=False)}" "</task_plan_completion>")
    if notification.elapsed_seconds is not None:
        lines.append(f"  <elapsed_seconds>{notification.elapsed_seconds:.1f}</elapsed_seconds>")
    if notification.provider_calls is not None:
        lines.append(f"  <provider_calls>{notification.provider_calls}</provider_calls>")
    if notification.total_tokens is not None:
        lines.append(f"  <total_tokens>{notification.total_tokens}</total_tokens>")
    lines.append(f"  <status>{escape(notification.status, quote=False)}</status>")
    lines.append(f"  <summary>{escape(notification.summary, quote=False)}</summary>")
    lines.append("</task_notification>")
    return "\n".join(lines)


def _summarize(job: BackgroundJob) -> str:
    if job.error is not None:
        return _truncate(job.error)
    if job.result is None:
        return "(no output)"
    content = job.result.content or "(empty result)"
    return _truncate(content)


def _truncate(text: str) -> str:
    if len(text) <= _SUMMARY_PREVIEW_LIMIT:
        return text
    return text[:_SUMMARY_PREVIEW_LIMIT] + "\n…(truncated; use background_status for the full result)"
