"""Agent 主循环最小闭环。"""

# ============================================================================
# 阅读路径导航 (Reading Path Guide)
# ============================================================================
# 这是 LansCoder 的核心编排器 (orchestrator)。
# AgentLoop 把用户输入、上下文投影 (projection)、provider 调用和工具执行串成一轮会话。
#
# 核心闭环 (一个 turn 的生命周期)：
#   1. 用户消息 → session.append_user_message() 写入 JSONL
#   2. session.rebuild_view() → ContextBuilder 投影成 provider messages
#   3. provider.complete(ChatRequest) 或 provider.astream(ChatRequest) → 模型返回 ChatResponse
#   4. 如果 response 有 tool_calls → ToolExecutor 执行 → tool_result 写回 JSONL
#   5. 回到步骤 2，直到模型不再调用工具 / 达到限制 / 需要用户输入
#
# AgentLoop 只协调"模型想做什么"和"会话事实怎样落库"，
# 具体协议转换交给 provider/context 层，具体工具执行交给 tools 层。
#
# → 上一步阅读：lanscoder/app/runtime.py (AgentChatRunner 创建 AgentLoop)
# → 下游依赖：providers/, context/, tools/, permissions/
# ============================================================================

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import partial
from typing import Any, Literal

import anyio

from lanscoder.runtime.cancellation import AgentCancelledError, CancellationToken
from lanscoder.runtime.user_input import UserInputRequest
from lanscoder.agent.ports import ContextManagerLike
from lanscoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason, _AgentLoopLimitReached
from lanscoder.agent.guardrails import TurnGuardrails
from lanscoder.agent.request_builder import PreparedMainRequest, RequestBuilder
from lanscoder.agent.session import AgentSession, PendingPermissionExecution
from lanscoder.agent.task_plan_policy import TaskPlanPolicy
from lanscoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from lanscoder.agent.tool_settlement import ToolCallSettlement
from lanscoder.agent.background import (
    DEFAULT_BACKGROUND_TOOL_NAMES,
    BackgroundJobManager,
    render_task_notification,
    with_background_controls,
)
from lanscoder.agent.user_input import (
    AgentTurnResult,
    AgentTurnStatus,
)
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.context.manager import ContextCompactRequest, ContextWindowTrigger
from lanscoder.input.attachments import UserAttachment
from lanscoder.permissions.types import PermissionDecision, PermissionDecisionKind, PermissionRequest
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatResponse, ChatStreamEvent, MainRequestOptions, ToolCall
from lanscoder.tools.permission_results import (
    make_permission_denied_result,
    make_prewrite_review_failed_result,
    make_prewrite_review_stale_result,
)
from lanscoder.tools.background import create_background_cancel_tool, create_background_status_tool
from lanscoder.agent.subagent import SubagentRunner
from lanscoder.tools.delegate import create_delegate_tool
from lanscoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result


class AgentLoop:
    """把用户输入、上下文投影、provider 调用和工具执行串成一轮会话。

    可以把这一层理解成 LansCoder 的“单轮事务”：

    1. 先把用户输入写入 append-only session log。
    2. 从 session log 重建当前视图，投影成 provider messages。
    3. 调用模型。如果模型返回普通文本，就写入 assistant 消息并结束。
    4. 如果模型返回 tool_calls，就先写入 assistant tool_call，再执行工具。
    5. 工具结果写成 role=tool 消息后，再次调用模型，让模型基于工具结果继续回答。

    这里故意不把具体工具、OpenAI SDK chunk、Textual widget 混进来。AgentLoop 只协调
    “模型想做什么”和“会话事实应该怎样落库”，具体协议转换交给 provider/context 层。
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        tools: list[Tool] | None = None,
        context_builder: ContextBuilder | None = None,
        context_manager: ContextManagerLike | None = None,
        limits: AgentLoopLimits | None = None,
        guardrails: TurnGuardrails | None = None,
        clock=time.monotonic,
        stream_event_handler: Callable[[ChatStreamEvent], None] | None = None,
        tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None,
        guidance_provider: Callable[[], list[str]] | None = None,
        cancellation_token: CancellationToken | None = None,
        request_options: MainRequestOptions | None = None,
        context_window: int | None = None,
        request_builder: RequestBuilder | None = None,
        background_manager: BackgroundJobManager | None = None,
        background_tool_names: frozenset[str] | None = None,
        enable_delegate_tool: bool = True,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        # -------- 阶段 1：核心依赖 (session / provider / 工具元数据) --------
        # session: 持久化 JSONL 写入与视图重建，是所有"事实落库"的入口
        # provider: 模型适配器，屏蔽 OpenAI/Anthropic/本地模型的协议差异
        # tool_settlement: 追踪哪些 tool_call 已被执行/跳过，避免重复结算
        # task_plan_policy: 任务计划策略，决定何时提示模型回顾任务进度
        self.session = session
        self.tool_settlement = ToolCallSettlement(session)
        self.task_plan_policy = TaskPlanPolicy(session)
        self.provider = provider
        self.request_options = request_options or MainRequestOptions()
        self.context_window = context_window

        # -------- 阶段 2：上下文投影 (projection) 与压缩 (compact) --------
        # context_builder: 把 session 视图投影成 provider 能理解的 messages
        # context_manager: 当投影超过 context_window 时，触发 compact（摘要/裁剪）
        # request_builder: 唯一的主请求构造入口；未显式注入时用与本 loop 相同的依赖兜底构造
        self.context_builder = context_builder or ContextBuilder()
        self.context_manager = context_manager
        self.request_builder = request_builder or RequestBuilder(
            session=session,
            provider=provider,
            context_builder=context_builder or ContextBuilder(),
            request_options=request_options or MainRequestOptions(),
            context_window=context_window,
        )

        # -------- 阶段 3：循环上限控制 (limits) --------
        # max_tool_rounds: 一次 turn 最多允许模型调用多少轮工具，防止无限循环
        # guardrails: turn 级预算策略，持有 provider 调用计数、turn 起始时间与 limit 响应构造
        self.limits = limits or AgentLoopLimits.default()
        self.max_tool_rounds = self.limits.max_tool_rounds
        self.clock = clock
        self.guardrails = guardrails or TurnGuardrails(provider=provider, limits=self.limits, clock=clock)

        # -------- 阶段 4：事件流回调（UI 层注入，AgentLoop 本身不消费）--------
        self.last_stream_events: list[ChatStreamEvent] = []
        self.stream_event_handler = stream_event_handler
        self.tool_event_handler = tool_event_handler
        self.guidance_provider = guidance_provider
        self.cancellation_token = cancellation_token
        self.progress_callback = progress_callback
        self._total_tokens = 0

        # -------- 阶段 5：后台任务管理 (background jobs) --------
        # background_manager: 管理长时运行的后台工具（如 grep 大仓库），可被取消/查询状态
        # background_tool_names: 哪些工具被视为"后台工具"，默认包含 background_run 等
        self.background_manager = background_manager
        self.background_tool_names = background_tool_names if background_tool_names is not None else DEFAULT_BACKGROUND_TOOL_NAMES
        self.enable_delegate_tool = enable_delegate_tool
        self._delegate_runner = None
        self._task_plan_reconciliation_attempted = False
        self._tool_rounds_completed = 0

        # -------- 阶段 6：工具注册（去重）--------
        # session 创建时通常已经注册了 session-scoped 工具。这里允许调用方再传入一批
        # 测试或临时工具，但避免重复注册同名工具导致模型 schema 不稳定。
        if tools:
            for tool in tools:
                if tool.name not in self.session.tool_registry.names():
                    self.session.tool_registry.register(tool)
        self._mcp_tool_names = {name for name in self.session.tool_registry.names() if name.startswith("mcp__")}
        self._active_mcp_tool_names: set[str] = set()

        # -------- 阶段 7：ToolExecutor 创建 --------
        # ToolExecutor 负责真正把 tool_call 变成 tool_result：权限检查、沙盒执行、事件回报
        # → 见 lanscoder/agent/tool_execution.py::ToolExecutor
        self.tool_executor = ToolExecutor(
            session=session,
            emit_event=self._emit_tool_event,
            check_cancelled=self._check_cancelled,
            cancellation_token=self.cancellation_token,
            validate_tool_call=self._validate_mcp_tool_call,
            observe_tool_result=self._observe_mcp_search_result,
            background_manager=self.background_manager,
            background_tool_names=self.background_tool_names,
        )

        # -------- 阶段 8：背景控制工具 + delegate 工具注册 --------
        # _ensure_background_control_tools(): 注册 background_status / background_cancel
        # _ensure_delegate_tool(): 注册 delegate 工具（让模型能启动子 agent）
        # → 见 lanscoder/tools/delegate.py::create_delegate_tool
        self._ensure_background_control_tools()
        self._ensure_delegate_tool()

    # ----------------------------------------------------------------------------
    # run_user_turn — 统一 async 入口
    # ----------------------------------------------------------------------------
    # 一个 turn = "用户说一句话 → 模型回答（可能带多轮工具调用）→ 返回最终 response"。
    # 根据 streaming 参数分流到流式实现或统一 async 核心（工具执行在 to_thread 上）。
    async def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
        streaming: bool = False,
    ) -> AgentTurnResult:
        """Execute one turn through the single asynchronous AgentTurnResult API."""

        if streaming:
            return await self._run_user_turn_streaming(content, attachments=attachments)
        return await self._run_user_turn_async(content, attachments=attachments)

    async def run_nudge_turn(self, *, streaming: bool = False) -> AgentTurnResult:
        """Execute one provider turn with no user message (subagent wake-up).

        后台子 agent 完成后，TUI 用它唤醒主 agent 汇报结果。与 run_user_turn 的区别是
        不写 user_message、不递增 current_turn；待投递的完成通知由
        _prepare_main_provider_request 里的 _append_background_notifications 投影成
        provider 的 user 内容。
        """

        if streaming:
            return await self._run_nudge_turn_streaming()
        return await self._run_nudge_turn_async()

    def replace_cancellation_token(self, token: CancellationToken | None) -> None:
        """Rebind cooperative cancellation when a paused turn resumes in the runner."""

        self.cancellation_token = token
        self.tool_executor.cancellation_token = token

    def clear_stream_events(self) -> None:
        self.last_stream_events = []

    # ============================================================================
    # _run_user_turn_sync — 供同步测试调用的薄 facade（内部转调 async 核心）
    # ============================================================================
    def _run_user_turn_sync(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        """Synchronous facade kept for legacy sync call sites."""

        return asyncio.run(self._run_user_turn_async(content, attachments=attachments))

    async def _run_user_turn_async(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        """统一 async 核心：非流式 turn 的完整启动流程。

        1. 检查 pending_permission_execution：上一轮可能因权限确认暂停，
           此时历史里已经有 assistant tool_call 等待 tool_result，不能再追加用户消息。
        2. _begin_turn()：重置 turn 级计数器（guardrails）与工具轮次。
        3. _repair_interrupted_tool_calls_before_provider_request()：修复上一轮意外中断
           留下的"有 tool_call 但缺 tool_result"的非法序列（补一条 canceled tool_result）。
        4. append_user_message()：把用户输入写入 JSONL。
        5. 进入 _run_tool_loop 核心循环（见下方）。
        """

        if self.session.pending_permission_execution is not None:
            # 上一轮已经把 assistant tool_call 写进历史，但还缺一个匹配的 tool_result。
            # 这种情况下不能追加新的用户消息，否则 provider 会看到非法消息序列。
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )

        self._begin_turn()  # 重置 guardrails 计数器和工具轮次
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self.session.append_user_message(content, attachments=attachments)  # 把用户消息写进jsonl

        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=False),
        )

    def _run_nudge_turn_sync(self) -> AgentTurnResult:
        """薄 sync facade：内部转调 async 唤醒轮次。"""

        return asyncio.run(self._run_nudge_turn_async())

    async def _run_nudge_turn_async(self) -> AgentTurnResult:
        """统一 async 核心：唤醒轮次，不追加用户消息，仅投递后台完成通知并跑工具循环。"""

        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )
        if self.background_manager is None or not self.background_manager.pending_completions(session_id=self.session.session_id):
            # 无待投递通知：不空转，避免模型被空输入唤醒。
            return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=None)

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=False),
        )

    # ============================================================================
    # resume_with_user_input — 权限确认恢复的异步入口
    # ============================================================================
    # 为什么权限恢复不能走普通用户消息路径？
    #   - 普通用户消息：追加一条新的 role=user 消息到历史。
    #   - 权限恢复：原始 tool_call 已经在历史里等待匹配的 tool_result，
    #     必须用 session 保存的原始 tool_call 来补齐 tool_result，不信任 UI 回传的参数。
    #     否则攻击者可以通过伪造 UI 回传让工具执行越权参数。
    # ============================================================================
    async def resume_with_user_input(
        self,
        request_id: str,
        answer: str,
        *,
        streaming: bool = False,
    ) -> AgentTurnResult:
        """Resume a paused turn through the single asynchronous result API."""

        if streaming:
            return await self._resume_with_user_input_streaming(request_id, answer)
        return await self._resume_with_user_input_async(request_id, answer)

    # ----------------------------------------------------------------------------
    # _resume_with_user_input_sync — 供同步测试调用的薄 facade（内部转调 async 核心）
    # ----------------------------------------------------------------------------
    def _resume_with_user_input_sync(self, request_id: str, answer: str) -> AgentTurnResult:
        """薄 sync facade：用用户回答恢复暂停的 turn。"""

        return asyncio.run(self._resume_with_user_input_async(request_id, answer))

    async def _resume_with_user_input_async(self, request_id: str, answer: str) -> AgentTurnResult:
        """统一 async 核心：用用户的回答恢复暂停的 turn。

        权限确认与 ask_user 都走这里（统一 pending 协议）：
          1. 校验超时 / 取消 / pending 状态存在
          2. _append_permission_resume_result：按答复决定 allow / deny / 预览过期，
             并续跑同批次剩余工具（deferred batch continuation）；若剩余里又有工具
             需要用户输入，链式返回新的 pending
          3. _begin_turn(new_user_turn=False)：重置 turn 计时但不重置 turn 序号
          4. 进入 _run_tool_loop 继续工具循环

        权限确认不能走”下一条用户消息”，因为模型原始 tool_call 已经在历史里等待一个
        匹配的 tool_result。ask_user 也统一走这里，这样回答后能继续执行同批次剩余
        工具，而不是把剩余工具写死成 skipped 让模型重新发起。
        """

        try:
            self.guardrails.check_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self.guardrails.limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self.guardrails.interrupted_response())
        result = await self._append_permission_resume_result(request_id, answer)
        if result is not None:
            return result
        self._begin_turn(new_user_turn=False)
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=False),
        )

    async def _resume_with_user_input_streaming(self, request_id: str, answer: str) -> AgentTurnResult:
        """流式模式下恢复权限确认，并继续消费 provider stream。"""

        try:
            self.guardrails.check_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self.guardrails.limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self.guardrails.interrupted_response())
        result = await self._append_permission_resume_result(request_id, answer)
        if result is not None:
            return result
        self._begin_turn(new_user_turn=False)
        self._check_cancelled()
        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=True),
        )

    async def _run_user_turn_streaming(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        """使用 provider 内部 stream event 协议执行一轮会话。

        文本 delta 可以被上层即时展示，但工具调用仍保持原子语义：只有 stream 完成并
        返回完整 `ChatResponse.tool_calls` 后，才写入 assistant message 并执行工具。
        """

        self.last_stream_events = []
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            pending_input = self.tool_executor.permission_input_request_from_pending(pending)
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=pending_input,
            )

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self.session.append_user_message(content, attachments=attachments)

        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=True),
        )

    async def _run_nudge_turn_streaming(self) -> AgentTurnResult:
        """streaming 版唤醒轮次：语义与同步版一致。"""

        self.last_stream_events = []
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            pending_input = self.tool_executor.permission_input_request_from_pending(pending)
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=pending_input,
            )
        if self.background_manager is None or not self.background_manager.pending_completions(session_id=self.session.session_id):
            return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=None)

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=True),
        )

    async def _append_permission_resume_result(self, request_id: str, answer: str) -> AgentTurnResult | None:
        """用回答恢复一个暂停的权限/ask_user。

        返回 AgentTurnResult 表示本轮必须立即结束（请求不存在，或延迟批次续跑时又
        遇到需要用户输入的工具，链式再暂停）；返回 None 表示整批执行完毕，调用方
        继续进入工具循环。
        """

        pending = self._pending_permission_for_resume(request_id)
        if isinstance(pending, AgentTurnResult):
            return pending
        result = self._prepare_permission_resume(pending, answer)
        if result is None:
            result = await anyio.to_thread.run_sync(self._execute_resumed_permission_tool_call, pending)
            self._emit_finished_permission_resume(pending, result)
        chained = await self._finish_permission_resume(pending, result)
        if chained is not None:
            return self._pending_turn_result(chained)
        return None

    def _pending_permission_for_resume(
        self,
        request_id: str,
    ) -> PendingPermissionExecution | AgentTurnResult:
        pending = self.session.pending_permission_execution
        if pending is None or pending.request_id != request_id:
            return AgentTurnResult(
                status=AgentTurnStatus.COMPLETED,
                response=ChatResponse(
                    provider=self.provider.name,
                    model=self.provider.model,
                    content="没有找到可恢复的权限确认请求。",
                    finish_reason="error",
                ),
            )
        if pending.kind == "permission_confirmation" and self.session.permission_manager is None:
            return AgentTurnResult(
                status=AgentTurnStatus.COMPLETED,
                response=ChatResponse(
                    provider=self.provider.name,
                    model=self.provider.model,
                    content="当前会话没有权限管理器，无法恢复权限确认。",
                    finish_reason="error",
                ),
            )
        return pending

    def _prepare_permission_resume(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ) -> ToolResult | None:
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
        self._check_cancelled()
        return None

    def _execute_resumed_permission_tool_call(self, pending: PendingPermissionExecution) -> ToolResult:
        # 用户同意后使用 session 保存的原始 tool_call，不能相信 UI 回传的参数。
        return self.tool_executor.execute_after_permission_with_cancellation_context(pending.tool_call)

    def _emit_finished_permission_resume(
        self,
        pending: PendingPermissionExecution,
        result: ToolResult,
    ) -> None:
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
        self.session.pending_permission_execution = None
        self.session.append_tool_result(tool_call=pending.tool_call, result=result)
        self._tool_rounds_completed += 1
        if not pending.deferred_tool_calls:
            return None
        execution = await self.tool_executor.execute_interactive_async(pending.deferred_tool_calls)
        return execution.pending_input

    def _resolve_pending_confirmation(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ):
        if not pending.review_only:
            return self.session.permission_manager.resolve_confirmation(pending.permission_request, answer)
        normalized = answer.strip().lower()
        if normalized in {"allow_once", "allow", "once", "2"}:
            current = self.session.preflight_tool_call_permission(pending.tool_call)
            if current is not None and current.decision.kind == PermissionDecisionKind.DENY:
                return current.decision
            return PermissionDecision(kind=PermissionDecisionKind.ALLOW, reason="用户批准应用已预览的修改。")
        if normalized in {"deny", "no", "1"} or normalized.startswith(("reject:", "reject_with_feedback:")):
            return self.session.permission_manager.resolve_confirmation(pending.permission_request, answer)
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"未知写前预览选择：{answer}",
        )

    def _blocked_permission_resume_result(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ) -> ToolResult | None:
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
            self.session.permission_manager.policy.project_root,
            access=self.session.sandbox_access,
        ):
            return None
        return make_prewrite_review_stale_result(
            tool_name=pending.tool_call.name,
            request=pending.permission_request,
        )

    # ============================================================================
    # _complete_once — 单次 provider 调用（不处理工具循环）
    # ============================================================================
    # 这是”问模型一次”的最小单元，拆出来后：
    #   - 同步调用（streaming=False，provider.complete 走 to_thread）、streaming 调用、
    #     prompt-too-long 恢复都能复用同一套上下文构造
    #   - 调用前后有明确的”检查点”序列，便于插入超时/取消/追踪逻辑
    #
    # 流程：
    #   1. _prepare_main_provider_request：构造 ChatRequest（含 projection / budget / compact）
    #   2. guardrails.reserve_call：检查是否达到 max_provider_calls 上限
    #   3. guardrails.check_timeout / _check_cancelled：超时或取消则抛异常提前退出
    #   4. provider.complete / provider.astream：实际调用模型 API
    #   5. _record_projection_consumed：记录本次投影被哪些 tool_result part 消费，
    #      便于后续 compact 判断哪些 tool_result 已经”被模型看过了”
    async def _complete_once(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
        streaming: bool,
    ) -> ChatResponse:
        """构造一次 provider 请求并获得模型响应（统一 sync/streaming）。

        streaming=True 时消费 provider.astream 并转发事件；streaming=False 时在
        worker 线程里跑 provider.complete，避免阻塞事件循环。
        """
        prepared = self._prepare_main_provider_request(
            tool_choice=tool_choice,
            runtime_instruction=runtime_instruction,
        )
        self.guardrails.reserve_call()
        self.guardrails.check_timeout()
        self._check_cancelled()
        if not streaming:
            response = await anyio.to_thread.run_sync(self.provider.complete, prepared.request)
        else:
            start_event_count = len(self.last_stream_events)
            final_response: ChatResponse | None = None
            try:
                async for event in self.provider.astream(prepared.request):
                    self._check_cancelled()
                    self.last_stream_events.append(event)
                    if self.stream_event_handler is not None:
                        self.stream_event_handler(event)
                    if event.kind == "message_completed":
                        final_response = event.response
                if final_response is None:
                    raise ProviderError(
                        ProviderErrorKind.API_ERROR,
                        "provider stream ended without message_completed event",
                    )
            except ProviderError:
                # 失败的 streaming 尝试不把已收到的局部 delta 当真实回答留给 UI
                del self.last_stream_events[start_event_count:]
                raise
            response = final_response
        self._record_projection_consumed(prepared)
        self._report_progress(response)
        return response

    async def _complete_once_with_recovery(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
        streaming: bool,
    ) -> ChatResponse:
        """统一 recovery：retryable 失败重试一次 → 回退同步 complete；prompt-too-long → compact 重试。"""
        retryable_failures = 0
        while True:
            try:
                return await self._complete_once(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                    streaming=streaming,
                )
            except ProviderError as exc:
                if exc.retryable:
                    if retryable_failures == 0:
                        retryable_failures += 1
                        continue
                    return await self._complete_once(
                        tool_choice=tool_choice,
                        runtime_instruction=runtime_instruction,
                        streaming=False,
                    )
                if not exc.requires_compaction:
                    raise
                result = await anyio.to_thread.run_sync(partial(self._compact_for_prompt_too_long, runtime_instruction=runtime_instruction))
                if result is None or result.status != "success":
                    raise
                return await self._complete_once(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                    streaming=streaming,
                )

    # ============================================================================
    # _run_tool_loop — 核心工具循环（本文件最关键的函数）
    # ============================================================================
    # 退出条件（三类，互斥）：
    #   (a) 模型返回的 response 没有 tool_calls：最终回答，直接 _complete_turn
    #   (b) 命中 max_tool_rounds：返回 tool_round_limit 响应
    #   (c) 某个工具需要用户输入 / 权限确认：返回 WAITING_FOR_USER_INPUT，等 UI 恢复
    #
    # 循环体（每一轮）：
    #   complete_once() → 得到 ChatResponse
    #     ├─ 如果无 tool_calls → 退出循环，这就是最终回答
    #     └─ 有 tool_calls → _continue_tool_loop_from_response 内循环
    #          1. 写 assistant tool_call 到 JSONL
    #          2. ToolExecutor.execute_interactive_async 执行所有 tool_call
    #          3. 写 tool_result 到 JSONL
    #          4. 递增 tool_rounds
    #          5. 再次 complete_once()，让模型基于工具结果继续
    #
    # 异常处理：
    #   - _AgentLoopLimitReached：达到 provider 调用上限 / turn 超时 → 优雅退出
    #   - AgentCancelledError：被外部 CancellationToken 取消 → 补中断 tool_result 后退出
    # ============================================================================
    async def _run_tool_loop(self, complete_once, *, initial_tool_choice="auto") -> AgentTurnResult:
        """核心工具循环：问模型，执行工具，再把工具结果回喂给模型。

        退出条件只有三类：
        - 模型返回的 response 没有 tool_calls：说明它已经给出最终回答。
        - 命中 max_tool_rounds：防止模型无限调用工具。
        - 某个工具需要用户输入或权限确认：暂停并把 pending_input 交给 UI。
        """

        guardrail_stop = False
        try:
            if self.max_tool_rounds is not None and self._tool_rounds_completed >= self.max_tool_rounds:
                return self._complete_turn(self.guardrails.limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT))
            response = self._drop_unsupported_tool_calls(await complete_once(tool_choice=initial_tool_choice))
            tool_rounds = self._tool_rounds_completed
            response, pending_input, tool_rounds = await self._continue_tool_loop_from_response(
                response,
                complete_once,
                tool_rounds,
            )
            if pending_input is not None:
                return self._pending_turn_result(pending_input)
            if response.finish_reason != AgentLoopStopReason.TOOL_ROUND_LIMIT.value:
                response, pending_input, _ = await self._run_task_plan_reconciliation_if_needed(
                    response,
                    complete_once,
                    tool_rounds,
                )
                if pending_input is not None:
                    return self._pending_turn_result(pending_input)
        except _AgentLoopLimitReached as exc:
            response = self.guardrails.limit_response(exc.reason)
            guardrail_stop = True
        except AgentCancelledError:
            self._append_interrupted_tool_results()
            response = self.guardrails.interrupted_response()

        if self._is_cancelled():
            self._append_interrupted_tool_results()
            response = self.guardrails.interrupted_response()
            return self._complete_turn(response)
        if guardrail_stop:
            return self._complete_turn(response)

        # 没有工具调用时，这条 response 就是最终 assistant 回复。命中轮次上限时也会写入
        # 一条纯文本说明，避免保存未执行的 tool_call。
        return self._complete_turn(response)

    @staticmethod
    def _pending_turn_result(pending_input: UserInputRequest) -> AgentTurnResult:
        return AgentTurnResult(status=AgentTurnStatus.WAITING_FOR_USER_INPUT, pending_input=pending_input)

    # ----------------------------------------------------------------------------
    # _complete_turn — turn 结束：把最终 response 写入 session，返回 AgentTurnResult
    # ----------------------------------------------------------------------------
    # 这是所有退出路径（正常回答 / 工具轮次上限 / 取消 / 超时）的统一出口。
    # append_assistant_response 把模型的最终回答落到 JSONL，保证会话可恢复。
    def _complete_turn(self, response: ChatResponse) -> AgentTurnResult:
        self.session.append_assistant_response(response)
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    # ----------------------------------------------------------------------------
    # _continue_tool_loop_from_response — 工具循环的内循环体
    # ----------------------------------------------------------------------------
    # 给定一个已包含 tool_calls 的 response，持续执行工具并回问模型，
    # 直到模型不再调用工具 / 命中轮次上限 / 需要用户输入。
    #
    # 关键顺序：必须先写 assistant tool_call，再写对应 tool_result。
    # 这是 provider 消息序列的合法性要求——OpenAI/Anthropic 都要求每条 tool result
    # 前面必须有对应的 assistant tool_call，否则下一次 complete 会报 400。
    async def _run_task_plan_reconciliation_if_needed(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        instruction = self._final_reconciliation_instruction()
        if instruction is None:
            return response, None, tool_rounds
        response = self._drop_unsupported_tool_calls(await complete_once(runtime_instruction=instruction))
        return await self._continue_tool_loop_from_response(response, complete_once, tool_rounds)

    def _final_reconciliation_instruction(self) -> str | None:
        if self._task_plan_reconciliation_attempted:
            return None
        instruction = self.task_plan_policy.final_reconciliation_instruction()
        if instruction is None:
            return None
        self._task_plan_reconciliation_attempted = True
        return instruction

    async def _continue_tool_loop_from_response(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        while response.tool_calls:
            self._check_cancelled()
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds

            self.session.append_assistant_response(response)
            execution = await self.tool_executor.execute_interactive_async(response.tool_calls)
            if execution.pending_input is not None:
                return response, execution.pending_input, tool_rounds

            tool_rounds += 1
            self._tool_rounds_completed = tool_rounds
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds
            self._check_cancelled()
            response = self._drop_unsupported_tool_calls(await complete_once())
        return response, None, tool_rounds

    def _append_interrupted_tool_results(self) -> None:
        self._emit_settlements("interrupted", self.tool_settlement.append_interrupted_tail())

    def _repair_interrupted_tool_calls_before_provider_request(self) -> None:
        self._emit_settlements("interrupted", self.tool_settlement.repair_before_provider_request())

    def _emit_settlements(self, kind, settlements) -> None:
        for settlement in settlements:
            self._emit_tool_event(kind, settlement.tool_call, result=settlement.result)

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
        prewrite_review: dict[str, object] | None = None,
    ) -> None:
        if self.tool_event_handler is None:
            return
        self.tool_event_handler(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
                prewrite_review=prewrite_review,
            )
        )

    # ============================================================================
    # _prepare_main_provider_request — 构造一次 provider 请求的全过程
    # ============================================================================
    # 这是"问模型"之前的所有准备工作。每一步都有明确副作用（写入 session / 触发 compact），
    # 顺序不能乱：
    #
    #   1. _repair_interrupted_tool_calls_before_provider_request：修复上一轮意外中断
    #      留下的非法 tool_call/tool_result 配对（补 canceled 结果）。
    #   2. _check_cancelled：协作式取消检查。
    #   3. _append_pending_guidance：把用户/系统通过 guidance_provider 注入的运行时提示
    #      作为 user 消息追加到历史（例如 "你现在的任务是…"）。
    #   4. _append_background_notifications：把后台任务完成/失败的通知作为 user 消息追加，
    #      让模型能感知到长时任务的进展。
    #   5. _provider_tool_definitions：从 session.tool_registry 取出当前可用工具的 schema。
    #   6. session.rebuild_view()：从 JSONL 重建当前会话视图（messages 列表）。
    #   7. request_builder.context_budget_for_view：计算当前视图占用的 token 预算。
    #   8. context_manager.compact_if_needed：如果预算超窗，触发自动 compact
    #      （摘要旧消息 / 裁剪 tool_result），再 rebuild 一次视图。
    #   9. request_builder.build：投影 messages、组装 ChatRequest，返回 PreparedMainRequest
    #      （含 request_id / fingerprint / 已消费 tool_result part ids）。
    def _prepare_main_provider_request(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> PreparedMainRequest:
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self._append_pending_guidance()
        self._append_background_notifications()
        definitions = self._provider_tool_definitions()
        view = self.session.rebuild_view()
        budget = self.request_builder.context_budget_for_view(
            view,
            runtime_instruction=runtime_instruction,
            definitions=definitions,
        )
        if self.context_manager is not None:
            result = self.context_manager.compact_if_needed(
                ContextCompactRequest(
                    view=view,
                    runtime_state=self.session.runtime_state,
                    budget=budget,
                    estimate_budget=lambda candidate: self.request_builder.context_budget_for_view(
                        candidate,
                        runtime_instruction=runtime_instruction,
                        definitions=definitions,
                    ),
                    trigger=ContextWindowTrigger.AUTO,
                    current_turn=self.session.current_turn,
                )
            )
            if result.status == "success":
                view = self.session.rebuild_view()

        return self.request_builder.build(
            view,
            definitions=definitions,
            tool_choice=tool_choice,
            runtime_instruction=runtime_instruction,
        )

    def _record_projection_consumed(self, prepared: PreparedMainRequest) -> None:
        self.session.record_provider_projection_consumed(
            request_id=prepared.request_id,
            projection_fingerprint=prepared.projection_fingerprint,
            part_ids=prepared.tool_result_part_ids,
            provider=self.provider.name,
            model=self.provider.model,
        )

    def _report_progress(self, response: ChatResponse) -> None:
        """累积 token 用量，再通知进度回调（若有）。

        用量累积不依赖进度回调，这样前台子 agent（无后台 job、无回调）也能通过
        ``usage_summary()`` 上报总量；后台子 agent 则同时把进度写给 TUI。
        """
        usage = response.usage
        if usage is not None and usage.total_tokens is not None:
            self._total_tokens += usage.total_tokens
        if self.progress_callback is None:
            return
        self.progress_callback(
            {
                "provider_calls": self.guardrails.call_count,
                "total_tokens": self._total_tokens,
            }
        )

    def usage_summary(self) -> dict[str, int]:
        """返回 loop 自创建以来累积的 provider 调用次数与 token 用量。"""
        return {
            "provider_calls": self.guardrails.call_count,
            "total_tokens": self._total_tokens,
        }

    def _compact_if_needed(
        self,
        *,
        trigger: ContextWindowTrigger,
        runtime_instruction: str | None = None,
    ):
        """把压缩触发交给 context manager。

        AgentLoop 不判断 token 细节，也不决定 L1/L2/L3 怎么做；它只在关键时机告诉
        context 层：“现在可能需要整理上下文了”。
        """

        if self.context_manager is None:
            return None
        definitions = self._provider_tool_definitions()
        view = self.session.rebuild_view()
        budget = self.request_builder.context_budget_for_view(
            view,
            runtime_instruction=runtime_instruction,
            definitions=definitions,
        )
        return self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=view,
                runtime_state=self.session.runtime_state,
                budget=budget,
                estimate_budget=lambda candidate: self.request_builder.context_budget_for_view(
                    candidate,
                    runtime_instruction=runtime_instruction,
                    definitions=definitions,
                ),
                trigger=trigger,
                current_turn=self.session.current_turn,
            )
        )

    def _compact_for_prompt_too_long(self, *, runtime_instruction: str | None = None):
        return self._compact_if_needed(
            trigger=ContextWindowTrigger.PROMPT_TOO_LONG,
            runtime_instruction=runtime_instruction,
        )

    def _provider_tool_definitions(self):
        """根据 provider 能力决定是否向模型暴露工具 schema。"""

        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            return []
        definitions = []
        for definition in self.session.tool_registry.definitions():
            if definition.name in HIDDEN_TOOL_STATUS_NAMES:
                continue
            if definition.name in self._mcp_tool_names and definition.name not in self._active_mcp_tool_names:
                continue
            definitions.append(self._augment_tool_definition(definition))
        return definitions

    def _loop_tool_definitions(self):
        """薄访问器：暴露当前 loop 视角的工具 schema。

        runtime 的 context_budget 依赖它，避免在 runtime 再造一份 augment/MCP 激活过滤
        逻辑；后续 MCP 激活追踪上移到独立协调器后，runtime 改从组装根取同一份定义。
        """

        return self._provider_tool_definitions()

    def _augment_tool_definition(self, definition):
        """给后台可用工具的 schema 附加 run_in_background/background_label 控制字段。

        只有在启用了 background_manager 且工具在允许列表里时才增强，避免向模型暴露它
        无法真正使用的控制字段。
        """

        if self.background_manager is None:
            return definition
        if definition.name not in self.background_tool_names:
            return definition
        return with_background_controls(definition)

    def _ensure_background_control_tools(self) -> None:
        """Register background_status/background_cancel whenever background runtime exists."""

        if self.background_manager is None:
            return
        names = set(self.session.tool_registry.names())
        if "background_status" not in names:
            self.session.tool_registry.register(create_background_status_tool(self.background_manager, session_id=self.session.session_id))
        if "background_cancel" not in names:
            self.session.tool_registry.register(create_background_cancel_tool(self.background_manager, session_id=self.session.session_id))

    def _ensure_delegate_tool(self) -> None:
        """Register the parent-facing delegate tool with a non-recursive child runner."""

        if not self.enable_delegate_tool:
            return
        if "delegate" in self.session.tool_registry.names():
            return
        project_root = None
        if self.session.permission_manager is not None:
            project_root = self.session.permission_manager.policy.project_root
        runner = SubagentRunner(
            store=self.session.store,
            provider=self.provider,
            tools=[tool for tool in self.session.tool_registry.tools() if tool.name != "delegate"],
            project_root=project_root,
            agents_md=self.session.agents_md,
            skill_catalog=self.session.skill_catalog,
            permission_manager=self.session.permission_manager,
            sandbox_access=self.session.sandbox_access,
            request_options=self.request_options,
            background_manager=self.background_manager,
        )
        self._delegate_runner = runner
        self.session.tool_registry.register(
            create_delegate_tool(
                runner,
                parent_session_id=self.session.session_id,
            )
        )

    def foreground_subagent(self) -> dict[str, Any] | None:
        """当前前台 delegate 子 agent 的实时进度（无则 None），供 TUI 在输入栏下方显示。"""
        if self._delegate_runner is None:
            return None
        return self._delegate_runner.foreground_progress

    def _begin_turn(self, *, new_user_turn: bool = True) -> None:
        if new_user_turn:
            self._active_mcp_tool_names.clear()
            self.guardrails.begin_turn()
            self._task_plan_reconciliation_attempted = False
            self._tool_rounds_completed = 0

    def _validate_mcp_tool_call(self, tool_call: ToolCall) -> ToolResult | None:
        if tool_call.name not in self._mcp_tool_names:
            return None
        if tool_call.name in self._active_mcp_tool_names:
            return None
        return make_error_result(
            tool_call.name,
            "MCP tool is not active for this user turn. Call mcp_tool_search first.",
            mcp_activation_required=True,
        )

    def _observe_mcp_search_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> None:
        if tool_call.name != "mcp_tool_search" or not result.ok:
            return
        payload = result.data.get("mcp_tool_search")
        if not isinstance(payload, dict):
            return
        activated = payload.get("activated_tools")
        if not isinstance(activated, list):
            return
        self._active_mcp_tool_names.update(name for name in activated if isinstance(name, str) and name in self._mcp_tool_names)

    def _append_pending_guidance(self) -> None:
        if self.guidance_provider is None:
            return
        guidance_items = self.guidance_provider()
        for content in guidance_items:
            text = content.strip()
            if text:
                self.session.append_user_message(text)

    def _append_background_notifications(self) -> None:
        """Drain finished background jobs into the history before calling the provider.

        Each completion becomes one independent ``background_notification`` user
        message.  It never reuses the original ``tool_call_id``, so the provider
        still sees exactly one tool result per assistant tool call.
        """

        if self.background_manager is None:
            return
        for notification in self.background_manager.collect_completed(session_id=self.session.session_id):
            self.session.append_background_notification(
                content=render_task_notification(notification),
                job_id=notification.job_id,
                tool_name=notification.tool_name,
                status=notification.status,
                task_id=notification.task_id,
                observed_revision=notification.observed_revision,
            )

    def _is_cancelled(self) -> bool:
        return self.cancellation_token is not None and self.cancellation_token.is_cancelled

    def _check_cancelled(self) -> None:
        if self.cancellation_token is not None:
            self.cancellation_token.raise_if_cancelled()

    def _drop_unsupported_tool_calls(self, response: ChatResponse) -> ChatResponse:
        """兜底保护：不支持工具的 provider 理论上不该返回 tool_calls。

        如果兼容站行为异常仍返回了 tool_calls，这里把它们丢弃并记录 diagnostics，避免
        agent 执行一个 provider 能力声明之外的工具链。
        """

        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is None or capabilities.supports_tools or not response.tool_calls:
            return response
        self._drop_unsupported_tool_call_stream_events()
        response.diagnostics.warnings.append("provider returned tool_calls even though supports_tools is false; tool calls were ignored")
        return ChatResponse(
            provider=response.provider,
            model=response.model,
            content=response.content or "当前 provider 不支持 tool calling，已忽略模型返回的工具调用。",
            tool_calls=[],
            finish_reason="error",
            usage=response.usage,
            diagnostics=response.diagnostics,
            raw=response.raw,
        )

    def _drop_unsupported_tool_call_stream_events(self) -> None:
        if not self.last_stream_events:
            return
        self.last_stream_events = [event for event in self.last_stream_events if event.kind not in {"tool_call_started", "tool_call_delta", "tool_call_completed"}]

    def _tool_round_limit_response(self, response: ChatResponse) -> ChatResponse:
        """工具轮次上限命中后，只保存纯文本说明，避免写入未执行的 tool_call。"""

        return self.guardrails.limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT, raw=response.raw)
