"""工具执行器:逐条执行工具调用,做权限预检、后台派发、并行只读批量执行并派发执行事件。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import anyio

if TYPE_CHECKING:
    from lanscoder.agent.observer import ToolEventSink

from lanscoder.runtime.cancellation import CancellationToken, cancellation_context
from lanscoder.agent.permission import PermissionCoordinator, PreparedPermission
from lanscoder.agent.session import AgentSession, PendingPermissionExecution
from lanscoder.agent.background import (
    BackgroundCapacityError,
    BackgroundJob,
    BackgroundJobManager,
    has_background_control_fields,
    make_background_placeholder_result,
    strip_background_controls,
)
from lanscoder.planning.reducer import TaskPlanCommandError, TaskPlanRevisionConflict
from lanscoder.planning.projection import ready_task_ids
from lanscoder.planning.service import TaskPlanService
from lanscoder.runtime.user_input import UserInputRequest, user_input_request_from_tool_result
from lanscoder.permissions.types import PermissionDecisionKind, PermissionMode, PermissionRequest
from lanscoder.providers.types import ToolCall
from lanscoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from lanscoder.tools.types import ToolResult, make_error_result
from lanscoder.subagent.types import role_allows_background, role_requires_worktree
from lanscoder.agent.worktree import WorktreeManager

PARALLEL_READONLY_TOOL_NAMES = frozenset(
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
    }
)
BYPASS_PARALLEL_TOOL_NAMES = PARALLEL_READONLY_TOOL_NAMES | frozenset(
    {
        "write",
        "edit",
        "delete",
        "apply_patch",
        "shell",
        "python_exec",
        "fetch",
        "web_search",
    }
)


@dataclass(frozen=True, slots=True)
class ToolExecutionEvent:
    """一次工具执行事件:类型、工具调用、结果与权限/预写审查信息。"""

    kind: Literal[
        "prewrite_review",
        "started",
        "finished",
        "permission_requested",
        "denied",
        "interrupted",
        "background_started",
    ]
    tool_call: ToolCall
    result: ToolResult | None = None
    permission_request: PermissionRequest | None = None
    prewrite_review: dict[str, object] | None = None


@dataclass(slots=True)
class ToolExecutionState:
    """工具执行的挂起状态(等待用户输入的请求)。"""

    pending_input: UserInputRequest | None = None


class ToolExecutor:
    """执行模型返回的工具调用序列:权限预检、后台/并行调度,并向观察者派发事件。"""

    def __init__(
        self,
        *,
        session: AgentSession,
        permission_coordinator: PermissionCoordinator,
        event_sink: ToolEventSink,
        cancellation_token: CancellationToken | None,
        validate_tool_call: Callable[[ToolCall], ToolResult | None] | None = None,
        observe_tool_result: Callable[[ToolCall, ToolResult], None] | None = None,
        background_manager: BackgroundJobManager | None = None,
        background_tool_names: frozenset[str] | None = None,
    ) -> None:
        """注入执行器依赖:会话、权限协调器、事件出口与后台管理器。"""
        self.session = session
        self._permission_coordinator = permission_coordinator
        self._event_sink = event_sink
        self.cancellation_token = cancellation_token
        self._validate_tool_call = validate_tool_call
        self._observe_tool_result = observe_tool_result
        self._background_manager = background_manager
        self._background_tool_names = background_tool_names
        self._background_request: dict[str, tuple[str | None, str | None]] = {}

    def _check_cancelled(self) -> None:
        if self.cancellation_token is not None:
            self.cancellation_token.raise_if_cancelled()

    def _emit_event(
        self,
        kind: Literal[
            "prewrite_review",
            "started",
            "finished",
            "permission_requested",
            "denied",
            "interrupted",
            "background_started",
        ],
        tool_call: ToolCall,
        *,
        result: ToolResult | None = None,
        permission_request: PermissionRequest | None = None,
        prewrite_review: dict[str, object] | None = None,
    ) -> None:
        """构造并派发单个工具执行事件到事件出口。"""
        self._event_sink.on_tool_event(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
                prewrite_review=prewrite_review,
            )
        )

    def execute_interactive(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """主入口:遍历工具调用,走校验/权限/后台/并行分支,遇等待输入时提前返回。"""

        state = ToolExecutionState()
        tool_calls, self._background_request = self._normalize_background_controls(tool_calls)
        index = 0
        while index < len(tool_calls):
            self._check_cancelled()
            tool_call = tool_calls[index]
            validation_error = self._validate_tool_call(tool_call) if self._validate_tool_call is not None else None
            if validation_error is not None:
                self._emit_event("denied", tool_call, result=validation_error)
                self._record_result(tool_call, validation_error)
                index += 1
                continue
            if tool_call.name in HIDDEN_TOOL_STATUS_NAMES:
                result = make_error_result(
                    tool_call.name,
                    f"内部控制面工具不可由主模型调用：{tool_call.name}",
                )
                self._emit_event("denied", tool_call, result=result)
                self._record_result(tool_call, result)
                index += 1
                continue
            permission = self._prepare_permission(tool_call, tool_calls[index + 1 :])
            if permission.result is not None:
                self._emit_event(
                    "denied",
                    tool_call,
                    result=permission.result,
                    permission_request=permission.permission_request,
                )
                self._record_result(tool_call, permission.result)
                index += 1
                continue
            if permission.pending_input is not None:
                self._emit_event(
                    "permission_requested",
                    tool_call,
                    permission_request=permission.permission_request,
                )
                state.pending_input = permission.pending_input
                return state
            if permission.prewrite_review is not None:
                self._emit_event(
                    "prewrite_review",
                    tool_call,
                    prewrite_review=permission.prewrite_review,
                )

            if tool_call.id in self._background_request:
                label, task_id = self._background_request[tool_call.id]
                result = self._dispatch_background(
                    tool_call,
                    label=label,
                    task_id=task_id,
                )
                self._record_result(tool_call, result)
                index += 1
                continue

            if self.can_execute_in_parallel(tool_call):
                batch_end = self.parallel_readonly_batch_end(tool_calls, index)
                results = self.execute_parallel_readonly_batch(tool_calls[index:batch_end])
                for batch_tool_call, result in zip(tool_calls[index:batch_end], results, strict=True):
                    self._record_result(batch_tool_call, result)
                index = batch_end
                continue

            result = self.execute_single(tool_call)
            pending_input = self._record_result(
                tool_call,
                result,
                deferred_tool_calls=tool_calls[index + 1 :],
            )
            if pending_input is not None:
                state.pending_input = pending_input
                return state
            index += 1
        return state

    async def execute_interactive_async(self, tool_calls: list[ToolCall]) -> ToolExecutionState:
        """在线程池中运行 execute_interactive。"""

        return await anyio.to_thread.run_sync(self.execute_interactive, tool_calls)

    def _normalize_background_controls(
        self,
        tool_calls: list[ToolCall],
    ) -> tuple[list[ToolCall], dict[str, tuple[str | None, str | None]]]:
        """剥离后台控制字段,返回清洗后的调用与后台请求映射。"""

        cleaned: list[ToolCall] = []
        requested: dict[str, tuple[str | None, str | None]] = {}
        for tool_call in tool_calls:
            if not has_background_control_fields(tool_call.arguments):
                cleaned.append(tool_call)
                continue
            clean_args, run_in_background, label, task_id = strip_background_controls(tool_call.arguments)
            cleaned.append(ToolCall(id=tool_call.id, name=tool_call.name, arguments=clean_args))
            if run_in_background:
                requested[tool_call.id] = (label, task_id)
        return cleaned, requested

    def _dispatch_background(
        self,
        tool_call: ToolCall,
        *,
        label: str | None,
        task_id: str | None,
    ) -> ToolResult:
        """把工具调用派发为后台任务,返回占位结果。"""

        if self._background_manager is None:
            return make_error_result(
                tool_call.name,
                "后台执行未启用；请去掉 run_in_background 后重试。",
                background_rejected="disabled",
            )
        observed_revision = self._validate_background_task_id(tool_call.name, task_id)
        if isinstance(observed_revision, ToolResult):
            return observed_revision
        allowed = self._background_tool_names
        if allowed is not None and tool_call.name not in allowed:
            return make_error_result(
                tool_call.name,
                f"工具 {tool_call.name} 不支持后台执行；请去掉 run_in_background 后重试。",
                background_rejected="not_allowed",
            )
        if tool_call.name == "delegate" and not self._delegate_call_allows_background(tool_call):
            return make_error_result(
                tool_call.name,
                "delegate 该角色不支持后台执行；仅 researcher/reviewer/tester/coder 可后台运行。",
                background_rejected="role_not_allowed",
            )
        if tool_call.name == "delegate" and self._delegate_call_requires_worktree(tool_call) and not self._worktree_isolation_available():
            return make_error_result(
                tool_call.name,
                "后台 coder 需要 git worktree 隔离，但当前项目不是 git 仓库；请在 git 仓库内使用，或改用前台 coder。",
                background_rejected="worktree_unavailable",
            )
        trusted_arguments = deepcopy(tool_call.arguments)
        if tool_call.name == "delegate" and self._delegate_call_requires_worktree(tool_call):
            if isinstance(trusted_arguments, dict):
                trusted_arguments = {**trusted_arguments, "isolate_worktree": True}
        trusted = ToolCall(id=tool_call.id, name=tool_call.name, arguments=trusted_arguments)

        def run() -> ToolResult:
            return self.session.execute_tool_call_after_permission_confirmation(trusted)

        def complete_task_plan(job: BackgroundJob) -> str | None:
            if job.task_id is not None:
                return self._mark_background_task_completed(
                    job.task_id,
                    observed_revision=job.observed_revision,
                )
            return None

        try:
            job = self._background_manager.start(
                run,
                session_id=self.session.session_id,
                tool_name=tool_call.name,
                label=label,
                task_id=task_id,
                observed_revision=observed_revision,
                dispatch_turn=self.session.current_turn,
                on_completed=complete_task_plan if task_id is not None else None,
            )
        except BackgroundCapacityError as exc:
            return make_error_result(
                tool_call.name,
                str(exc),
                background_rejected="capacity",
            )
        self._emit_event("background_started", tool_call)
        return make_background_placeholder_result(job)

    def _task_plan_service(self) -> TaskPlanService:
        return TaskPlanService(store=self.session.store, writer=self.session.writer)

    def _validate_background_task_id(self, tool_name: str, task_id: str | None) -> int | ToolResult | None:
        """校验后台任务引用的 task_id 是否有效,返回计划修订号或错误。"""
        if task_id is None:
            return None
        plan = self._task_plan_service().current()
        if plan is None:
            return make_error_result(
                tool_name,
                f"Cannot start background work for task_id {task_id!r}: no current task plan. " "Call task_create first, or remove task_id.",
                background_rejected="task_plan_missing",
                task_id=task_id,
            )
        if not any(task.id == task_id for task in plan.tasks):
            return make_error_result(
                tool_name,
                f"Cannot start background work: task_id {task_id!r} is not in the current task plan. " "Call task_list, then retry with an existing task ID.",
                background_rejected="task_not_found",
                task_id=task_id,
                actual_revision=plan.revision,
            )
        return plan.revision

    def _mark_background_task_completed(self, task_id: str, *, observed_revision: int | None) -> str:
        """把后台任务完成状态回写到任务计划(带冲突重试)。"""

        service = self._task_plan_service()
        for _ in range(3):
            plan = service.current()
            if plan is None:
                return f"TaskPlan task {task_id!r} was not updated because no plan is current."
            task = next((candidate for candidate in plan.tasks if candidate.id == task_id), None)
            if task is None:
                return f"TaskPlan task {task_id!r} was not updated because it no longer exists."
            if task.status in {"completed", "cancelled"}:
                return f"TaskPlan task {task_id!r} was not updated because it is already {task.status}."
            try:
                if task.status == "pending":
                    if plan.revision != observed_revision:
                        return f"TaskPlan task {task_id!r} was not updated because it returned to pending " "after this background job started."
                    if task_id not in ready_task_ids(plan):
                        return f"TaskPlan task {task_id!r} was not updated because it is pending and blocked."
                    plan = service.update(
                        expected_revision=plan.revision,
                        updates=[{"id": task_id, "status": "in_progress"}],
                    ).plan
                service.update(
                    expected_revision=plan.revision,
                    updates=[{"id": task_id, "status": "completed"}],
                )
            except TaskPlanRevisionConflict:
                continue
            except TaskPlanCommandError:
                return f"TaskPlan task {task_id!r} was not updated because its latest state rejects completion."
            return f"TaskPlan task {task_id!r} completed."
        return f"TaskPlan task {task_id!r} was not updated because the plan changed concurrently."

    def _delegate_call_allows_background(self, tool_call: ToolCall) -> bool:
        """delegate 调用的角色是否允许后台执行。"""
        arguments = tool_call.arguments
        if not isinstance(arguments, dict):
            return False
        return role_allows_background(str(arguments.get("role") or ""))

    def _delegate_call_requires_worktree(self, tool_call: ToolCall) -> bool:
        """delegate 调用的角色是否要求 worktree 隔离。"""
        arguments = tool_call.arguments
        if not isinstance(arguments, dict):
            return False
        return role_requires_worktree(str(arguments.get("role") or ""))

    def _worktree_isolation_available(self) -> bool:
        """判断当前项目是否具备 worktree 隔离能力。"""
        manager = self._permission_coordinator.permission_manager
        if manager is None:
            return False
        return WorktreeManager(manager.policy.project_root).available()

    def _prepare_permission(
        self,
        tool_call: ToolCall,
        deferred_tool_calls: list[ToolCall],
    ) -> PreparedPermission:
        """为工具调用做权限准备,返回预检结果。"""

        return self._permission_coordinator.prepare(tool_call, deferred_tool_calls)

    def _record_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
        *,
        deferred_tool_calls: list[ToolCall] | None = None,
    ) -> UserInputRequest | None:
        """记录工具结果;遇到 ask_user 则挂起并返回输入请求。"""

        pending_input = user_input_request_from_tool_result(
            result,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
        )
        if pending_input is not None:
            self._permission_coordinator.store_pending_ask_user(
                tool_call=tool_call,
                deferred_tool_calls=deferred_tool_calls or [],
                user_input_request=pending_input,
            )
            return pending_input
        self.session.append_tool_result(tool_call=tool_call, result=result)
        if self._observe_tool_result is not None:
            self._observe_tool_result(tool_call, result)
        return None

    def parallel_readonly_batch_end(self, tool_calls: list[ToolCall], start: int) -> int:
        """找出从 start 起连续可并行执行的工具调用边界。"""
        end = start
        while end < len(tool_calls) and self.can_execute_in_parallel(tool_calls[end]):
            end += 1
        return end

    def can_execute_in_parallel(self, tool_call: ToolCall) -> bool:
        """判断单个工具调用能否并行执行。"""
        if self._permission_coordinator.requires_bypass_review(tool_call):
            return False
        if tool_call.id in self._background_request:
            return False
        if tool_call.name not in self.parallel_tool_names_for_current_mode():
            return False
        preflight = self._permission_coordinator.preflight(tool_call)
        return preflight is None or preflight.decision.kind == PermissionDecisionKind.ALLOW

    def parallel_tool_names_for_current_mode(self) -> frozenset[str]:
        """按当前权限模式返回可并行执行的工具集合。"""
        manager = self._permission_coordinator.permission_manager
        if manager is not None and manager.mode == PermissionMode.BYPASS:
            return BYPASS_PARALLEL_TOOL_NAMES
        return PARALLEL_READONLY_TOOL_NAMES

    def execute_single(self, tool_call: ToolCall) -> ToolResult:
        """单线程执行一个工具调用并派发 started/finished 事件。"""
        self._check_cancelled()
        self._emit_event("started", tool_call)
        with cancellation_context(self.cancellation_token):
            result = self.session.execute_tool_call(tool_call)
        self._emit_event("finished", tool_call, result=result)
        self._check_cancelled()
        return result

    def execute_parallel_readonly_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """用线程池并行执行一批只读工具调用。"""
        self._check_cancelled()
        for tool_call in tool_calls:
            self._emit_event("started", tool_call)
        with ThreadPoolExecutor(max_workers=len(tool_calls)) as executor:
            results = list(executor.map(self.execute_with_cancellation_context, tool_calls))
        for tool_call, result in zip(tool_calls, results, strict=True):
            self._emit_event("finished", tool_call, result=result)
        self._check_cancelled()
        return results

    def execute_with_cancellation_context(self, tool_call: ToolCall) -> ToolResult:
        """带取消上下文执行一次工具调用。"""
        self._check_cancelled()
        with cancellation_context(self.cancellation_token):
            return self.session.execute_tool_call(tool_call)

    def execute_after_permission_with_cancellation_context(self, tool_call: ToolCall) -> ToolResult:
        """权限确认后带取消上下文执行工具调用。"""
        self._check_cancelled()
        with cancellation_context(self.cancellation_token):
            return self.session.execute_tool_call_after_permission_confirmation(tool_call)

    def permission_input_request_from_pending(self, pending: PendingPermissionExecution) -> UserInputRequest:
        """从挂起权限执行构造用户输入请求,缺失时抛错。"""
        confirmation = self.session.pending_permission_input_request(pending)
        if confirmation is None:
            raise RuntimeError("permission confirmation requires a pending request and permission manager")
        return confirmation
