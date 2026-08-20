"""权限确认 / ask_user 恢复子流程。

Task 6 把原先 AgentLoop 里的 8 个 resume 方法抽到这里，loop 只保留入口 glue。
``handle`` 把一次恢复判定成三种结果（三值判别式）：

- ``continue``：整批（含延迟批次）跑完，loop 应重新进入工具循环。
- ``wait_for_input``：延迟批次里又有工具需要用户输入，链式暂停，
  ``result`` 携带 ``AgentTurnResult(WAITING_FOR_USER_INPUT)``。
- ``finished``：没有匹配的 pending 请求（或没有权限管理器），
  ``result`` 携带 ``AgentTurnResult(COMPLETED, finish_reason="error")``。

pending 查找只经过注入的最小 ``PendingStore`` 协议；实现由
``CoordinatorPendingStore`` 走 ``PermissionCoordinator`` 的 pending_get/pending_clear，
handler 逻辑不感知 store 实现差异。
"""

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
from lanscoder.runtime.user_input import UserInputRequest
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
    """一次权限恢复的判别结果。"""

    kind: Literal["continue", "wait_for_input", "finished"]
    result: AgentTurnResult | None = None


class PendingStore(Protocol):
    """handler 访问 pending 状态的最小协议（经 coordinator-backed 实现）。"""

    def get(self, request_id: str) -> PendingPermissionExecution | None: ...
    def clear(self) -> None: ...


class CoordinatorPendingStore:
    """把 coordinator 的 pending 状态薄适配成最小 PendingStore 协议。

    ``get`` 走 ``coordinator.pending_get``，``clear`` 走 ``coordinator.pending_clear``；
    handler 逻辑不感知 store 实现差异。
    """

    def __init__(self, coordinator: PermissionCoordinator) -> None:
        self._coordinator = coordinator

    def get(self, request_id: str) -> PendingPermissionExecution | None:
        return self._coordinator.pending_get(request_id)

    def clear(self) -> None:
        self._coordinator.pending_clear()


class PermissionResumeHandler:
    """用用户的回答恢复一个暂停的权限确认或 ask_user。

    恢复成功后按需续跑同批次剩余工具（deferred batch continuation）：剩余工具
    作为新一批交给 ToolExecutor，全部跑完返回 ``continue``；若又有工具需要用户
    输入，链式暂停并返回 ``wait_for_input``。
    """

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
        """组装根在 loop 构造完成后把工具轮次计数回调绑到 loop。"""

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
            # ask_user 的回答就是最终 tool_result，无需执行工具（暂停时已 emit
            # started/finished，这里不重复 emit）。问题保存在 ask_user_request 里，
            # 供模型与转录参考。
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
        # 用户同意后使用 session 保存的原始 tool_call，不能相信 UI 回传的参数。
        return self._tool_executor.execute_after_permission_with_cancellation_context(pending.tool_call)

    def _emit_finished_permission_resume(self, pending: PendingPermissionExecution, result: ToolResult) -> None:
        self._emit_tool_event(
            "finished",
            pending.tool_call,
            result=result,
            permission_request=pending.permission_request,
        )

    async def _finish_permission_resume(self, pending: PendingPermissionExecution, result: ToolResult) -> UserInputRequest | None:
        """写回已恢复工具的 result，续跑同批次剩余工具。

        剩余工具（``deferred_tool_calls``）作为新一批交给 ToolExecutor 继续执行：
        - 全部跑完：返回 None，调用方进入工具循环回问模型。
        - 又有工具需要用户输入：返回链式 pending，本轮立即暂停等下一次回答。
        """
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
