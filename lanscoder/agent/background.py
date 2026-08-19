"""通用异步工具运行时（Phase 1）。

这个模块只负责“把一个同步工具执行放到后台线程里跑，并在完成后产出一条可注入的
notification”。它刻意不认识 AgentLoop / ToolExecutor / session，只依赖工具层的
`ToolResult` 和 provider 层的 `ToolDefinition`，这样既能被 executor 调用，也能被
单元测试独立驱动。

关键协议约束（与 docs/async-subagents-dag-plan.md 对齐）：

- 后台执行不代表“让原始 tool_call 悬空”。原始 `tool_call_id` 必须立刻得到一条
  占位 `tool_result`（由 ToolExecutor 负责写入）。
- 真正的完成结果稍后作为独立的 `<task_notification>` 用户消息注入，绝不会再产生
  第二条同 id 的 `tool_result`。
"""

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
from lanscoder.runtime.cancellation import CancellationToken, cancellation_context
from lanscoder.tools.types import ToolResult, make_text_result

# ---------------------------------------------------------------------------
# 控制面字段（control-plane）
# ---------------------------------------------------------------------------

RUN_IN_BACKGROUND_ARG = "run_in_background"
BACKGROUND_LABEL_ARG = "background_label"
BACKGROUND_TASK_ID_ARG = "task_id"
BACKGROUND_CONTROL_ARGS = frozenset({RUN_IN_BACKGROUND_ARG, BACKGROUND_LABEL_ARG, BACKGROUND_TASK_ID_ARG})

# Phase 1 默认允许后台化的工具：只读探查 + 已做权限预检的执行/网络工具。
# 刻意排除 TaskPlan 写入、task_boundary / ask_user（控制面）以及全部写入类工具（要等
# worktree 隔离），避免后台任务在没有安全边界时改动主工作区或伪造用户交互。
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

# 后台任务状态字面量。用字符串而不是枚举，方便直接进 JSONL / notification。
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_SUMMARY_PREVIEW_LIMIT = 2000


def with_background_controls(definition: ToolDefinition) -> ToolDefinition:
    """返回一个附加后台控制字段（含可选 TaskPlan ``task_id``）的 schema 副本。

    只增字段、不改原定义，避免污染 registry 里存的工具。executor 会在真正调用
    executor 前把这些控制面字段剥掉。
    """

    parameters: dict[str, Any] = deepcopy(definition.parameters) if definition.parameters else {}
    parameters.setdefault("type", "object")
    properties = parameters.setdefault("properties", {})
    if not isinstance(properties, dict):  # 防御：异常 schema 不做增强
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
    """从工具参数里分离控制面字段。

    返回 `(clean_arguments, run_in_background, label, task_id)`。非 dict 参数
    原样透传，`run_in_background=False`，让调用方走普通同步路径。
    """

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
    """判断参数里是否带有任一控制面字段（不管真假值）。"""

    return isinstance(arguments, dict) and any(key in arguments for key in BACKGROUND_CONTROL_ARGS)


@dataclass(slots=True)
class BackgroundNotification:
    """一次后台任务完成后要回喂给模型的结构化摘要。"""

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
    kind: str = "tool"


@dataclass(slots=True)
class BackgroundJob:
    """后台任务的运行期记录。"""

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
    # Cross-thread progress tracking for the TUI activity panel.
    # Written by the subagent thread, read by the TUI timer (protected by
    # BackgroundJobManager._lock).
    progress: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        """给 background_status 工具用的可读快照。"""

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


# Thread-local that BackgroundJobManager._run sets before calling the job function
# so that subagent code can discover which background job it belongs to and
# report progress back via BackgroundJob.progress.
_current_job_id: threading.local = threading.local()


def current_job_id() -> str | None:
    """Return the background job ID for the calling thread, or None."""
    return getattr(_current_job_id, "value", None)


class BackgroundJobManager:
    """内存态后台任务管理器。

    - 用 `ThreadPoolExecutor` 跑同步工具执行函数（Phase 1 只接非写入工具）。
    - id 递增且确定（`bg_0001`...），方便测试与人读。
    - 完成的任务进入 `_completed` 队列，等 AgentLoop 在下次 provider 请求前收集，
      转成 `<task_notification>` 用户消息。
    """

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
        """Register a callback invoked (in the job's thread) when a job finishes."""
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
        """登记并调度一个后台任务。

        `func` 必须是一个无参、返回 `ToolResult` 的可调用；executor 通常把它包成
        “执行剥离控制字段后的 tool_call”。若已达并发上限会抛 `BackgroundCapacityError`，
        由调用方转成普通错误结果告诉模型。
        """

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
        except Exception as exc:  # noqa: BLE001 - 后台失败必须转成 notification，而不是吞掉
            self._finish(job, result=None, error=f"后台任务执行失败：{exc}")
            return
        self._finish(job, result=result, error=None)

    def _finish(self, job: BackgroundJob, *, result: ToolResult | None, error: str | None) -> None:
        abandoned_cleanup: Callable[[], None] | None = None
        with self._lock:
            if job.status == STATUS_CANCELLED:
                # 已被显式取消：保留取消状态，但仍记录迟到结果，避免悬空。
                job.result = result
                self._futures.pop(job.id, None)
                return
            if job.abandoned:
                # 该 job 在 recall 后已被丢弃：结果不再投递、也不再触发完成回调，
                # 但若它已创建隔离 worktree，则清理之，避免留下孤儿。
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

        # Fire the callback outside the lock to avoid deadlocks.
        cb = self._on_job_completed
        if cb is not None:
            cb(job)

    @staticmethod
    def _invoke_worktree_cleanup(cleanup: Callable[[], None]) -> None:
        """Best-effort worktree teardown; failures must never break the loop."""

        try:
            cleanup()
        except Exception:  # noqa: BLE001 - orphan cleanup is opportunistic.
            pass

    def _notification_for(self, job: BackgroundJob) -> BackgroundNotification:
        summary = _summarize(job)
        ok = job.status == STATUS_COMPLETED
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
        )

    # -- 查询/收集 --------------------------------------------------------

    def collect_completed(self, *, session_id: str | None = None) -> list[BackgroundNotification]:
        """Finalize and return completed jobs from a serialized consumer point.

        Worker threads only determine the tool's final status.  A linked TaskPlan
        completion callback runs here, immediately before the caller persists the
        notification, so it never races normal session event writes.
        """

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
        """Return currently running jobs (for the TUI activity panel)."""
        with self._lock:
            return [job for job in self._jobs.values() if job.status == STATUS_RUNNING]

    def pending_completions(self, *, session_id: str | None = None) -> list[BackgroundJob]:
        """Return completed jobs that have not yet been collected.

        Unlike :meth:`collect_completed`, this peeks without consuming the
        ``_completed`` queue.  The TUI uses it to decide whether to wake the
        main agent after a turn finishes while background results are still
        undelivered.  The loop's own drain stays the single consumer.
        """

        with self._lock:
            return [
                job for job in self._completed
                if session_id is None or job.session_id == session_id
            ]

    def cancel(self, job_id: str, *, session_id: str | None = None) -> BackgroundJob | None:
        """尽力取消一个后台任务。

        - 还没开始执行：直接 `future.cancel()`，标记 cancelled 并产出一条通知。
        - 已在运行：设置协作取消 token；能否真正停下取决于工具是否检查取消。
        """

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
        """丢弃某个 session 在给定 turn 及之后派发的后台任务。

        /recall 回退后，被截掉的那几轮里派发的 job 已失去上下文（其占位 tool_result
        已随截断消失），继续投递完成通知会变成"幽灵通知"。这里把未投递的完成项从
        队列移除，并取消仍在运行的 job，使其结果既不入队、也不触发完成回调。

        Returns:
            被丢弃的 job 数量。
        """

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
        """阻塞直到当前已知的 future 全部结束。主要给测试用。

        返回是否在超时前全部完成。
        """

        with self._lock:
            futures = list(self._futures.values())
        if not futures:
            return True
        _, not_done = futures_wait(futures, timeout=timeout)
        return not not_done

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class BackgroundCapacityError(RuntimeError):
    """后台任务数量已达上限。"""

    def __init__(self, max_jobs: int) -> None:
        super().__init__(f"后台任务已达上限（{max_jobs}）。请等待现有任务完成或取消后再试。")
        self.max_jobs = max_jobs


def make_background_placeholder_result(job: BackgroundJob) -> ToolResult:
    """为原始 tool_call 生成立即返回的占位结果。

    这条结果闭合了 provider 历史里的 tool_call，且刻意不带 requires_user_input，
    不会让 agent loop 暂停。真正结果稍后由 notification 送达。
    """

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
    """把完成事件渲染成注入模型的 `<task_notification>` 文本。"""

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
