from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import anyio

from lanscoder.agent.observer import TurnObserver
from lanscoder.agent.session import AgentSession, PendingPermissionExecution
from lanscoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from lanscoder.agent.user_input import AgentTurnResult, AgentTurnStatus
from lanscoder.permissions.types import PermissionDecision, PermissionDecisionKind, PermissionRequest
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatResponse, ToolCall
from lanscoder.permissions.user_input import UserInputRequest
from lanscoder.tools.permission_results import (
    make_permission_denied_result,
    make_prewrite_review_failed_result,
    make_prewrite_review_stale_result,
)
from lanscoder.tools.types import ToolResult, make_text_result

if TYPE_CHECKING:
    from lanscoder.agent.permission import PermissionCoordinator


@dataclass(frozen=True)
class ResumeOutcome:

    kind: Literal["continue", "wait_for_input", "finished"]
    result: AgentTurnResult | None = None


class PendingStore(Protocol):

    def get(self, request_id: str) -> PendingPermissionExecution | None: ...
    def clear(self) -> None: ...


class CoordinatorPendingStore:

    def __init__(self, coordinator: PermissionCoordinator) -> None:
        self._coordinator = coordinator

    def get(self, request_id: str) -> PendingPermissionExecution | None:
        return self._coordinator.pending_get(request_id)

    def clear(self) -> None:
        self._coordinator.pending_clear()


class PermissionResumeHandler:

    def __init__(
        self,
        *,
        pending_store: PendingStore,
        provider: ChatProvider,
        tool_executor: ToolExecutor,
        observer: TurnObserver,
        session: AgentSession,
        permission_coordinator: PermissionCoordinator,
        on_tool_round_completed: Callable[[], None],
    ) -> None:
        self._pending_store = pending_store
        self._provider = provider
        self._tool_executor = tool_executor
        self._observer = observer
        self._session = session
        self._permission_coordinator = permission_coordinator
        self._on_tool_round_completed = on_tool_round_completed

    def set_tool_round_callback(self, callback: Callable[[], None]) -> None:

        self._on_tool_round_completed = callback

    async def handle(self, request_id: str, answer: str) -> ResumeOutcome:
        pending = self._pending_permission_for_resume(request_id)
        if isinstance(pending, AgentTurnResult):
            return ResumeOutcome(kind="finished", result=pending)
        result = self._prepare_permission_resume(pending, answer)
        if result is None:
            result = await anyio.to_thread.run_sync(self._execute_resumed_permission_tool_call, pending)
            self._emit_finished_permission_resume(pending, result)
        chained = await self._finish_permission_resume(pending, result)
        if chained is not None:
            return ResumeOutcome(
                kind="wait_for_input",
                result=AgentTurnResult(status=AgentTurnStatus.WAITING_FOR_USER_INPUT, pending_input=chained),
            )
        return ResumeOutcome(kind="continue")

    def _pending_permission_for_resume(self, request_id: str) -> PendingPermissionExecution | AgentTurnResult:
        pending = self._pending_store.get(request_id)
        if pending is None:
            return AgentTurnResult(
                status=AgentTurnStatus.COMPLETED,
                response=ChatResponse(
                    provider=self._provider.name,
                    model=self._provider.model,
                    content="没有找到可恢复的权限确认请求。",
                    finish_reason="error",
                ),
            )
        if pending.kind == "permission_confirmation" and self._permission_coordinator.permission_manager is None:
            return AgentTurnResult(
                status=AgentTurnStatus.COMPLETED,
                response=ChatResponse(
                    provider=self._provider.name,
                    model=self._provider.model,
                    content="当前会话没有权限管理器，无法恢复权限确认。",
                    finish_reason="error",
                ),
            )
        return pending

    def _prepare_permission_resume(self, pending: PendingPermissionExecution, answer: str) -> ToolResult | None:
        if pending.kind == "ask_user":
            return make_text_result(
                "ask_user",
                answer,
                question=str(pending.ask_user_request.question if pending.ask_user_request is not None else ""),
                answer=answer,
            )
        result = self._blocked_permission_resume_result(pending, answer)
        if result is not None:
            self._emit_tool_event(
                "denied",
                pending.tool_call,
                result=result,
                permission_request=pending.permission_request,
            )
            return result
        self._emit_tool_event(
            "started",
            pending.tool_call,
            permission_request=pending.permission_request,
        )
        return None

    def _execute_resumed_permission_tool_call(self, pending: PendingPermissionExecution) -> ToolResult:
        return self._tool_executor.execute_after_permission_with_cancellation_context(pending.tool_call)

    def _emit_finished_permission_resume(self, pending: PendingPermissionExecution, result: ToolResult) -> None:
        self._emit_tool_event(
            "finished",
            pending.tool_call,
            result=result,
            permission_request=pending.permission_request,
        )

    async def _finish_permission_resume(self, pending: PendingPermissionExecution, result: ToolResult) -> UserInputRequest | None:
        self._pending_store.clear()
        self._session.append_tool_result(tool_call=pending.tool_call, result=result)
        self._on_tool_round_completed()
        if not pending.deferred_tool_calls:
            return None
        execution = await self._tool_executor.execute_interactive_async(pending.deferred_tool_calls)
        return execution.pending_input

    def _resolve_pending_confirmation(self, pending: PendingPermissionExecution, answer: str) -> PermissionDecision:
        if not pending.review_only:
            return self._permission_coordinator.permission_manager.resolve_confirmation(pending.permission_request, answer)
        normalized = answer.strip().lower()
        if normalized in {"allow_once", "allow", "once", "2"}:
            current = self._permission_coordinator.preflight(pending.tool_call)
            if current is not None and current.decision.kind == PermissionDecisionKind.DENY:
                return current.decision
            return PermissionDecision(kind=PermissionDecisionKind.ALLOW, reason="用户批准应用已预览的修改。")
        if normalized in {"deny", "no", "1"} or normalized.startswith(("reject:", "reject_with_feedback:")):
            return self._permission_coordinator.permission_manager.resolve_confirmation(pending.permission_request, answer)
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"未知写前预览选择：{answer}",
        )

    def _blocked_permission_resume_result(self, pending: PendingPermissionExecution, answer: str) -> ToolResult | None:
        decision = self._resolve_pending_confirmation(pending, answer)
        if decision.kind == PermissionDecisionKind.DENY:
            return make_permission_denied_result(
                tool_name=pending.tool_call.name,
                request=pending.permission_request,
                decision=decision,
            )
        if pending.prewrite_review is None:
            return None
        if not pending.prewrite_review.ok:
            return make_prewrite_review_failed_result(
                tool_name=pending.tool_call.name,
                request=pending.permission_request,
                error=pending.prewrite_review.error or "未知错误",
            )
        if pending.prewrite_review.is_current(
            self._permission_coordinator.permission_manager.policy.project_root,
            access=self._permission_coordinator.sandbox_access,
        ):
            return None
        return make_prewrite_review_stale_result(
            tool_name=pending.tool_call.name,
            request=pending.permission_request,
        )

    def _emit_tool_event(
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
    ) -> None:
        self._observer.on_tool_event(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
            )
        )
