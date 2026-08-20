from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, Literal

import anyio

from lanscoder.runtime.cancellation import AgentCancelledError, CancellationToken
from lanscoder.runtime.user_input import UserInputRequest
from lanscoder.agent.ports import ContextManagerLike
from lanscoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason, _AgentLoopLimitReached
from lanscoder.agent.guardrails import TurnGuardrails
from lanscoder.agent.mcp_activation import McpActivationTracker
from lanscoder.agent.observer import TurnObserver
from lanscoder.agent.permission_resume import PermissionResumeHandler
from lanscoder.agent.request_builder import PreparedMainRequest, RequestBuilder
from lanscoder.agent.session import AgentSession
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
from lanscoder.permissions.types import PermissionRequest
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatResponse, ChatStreamEvent, MainRequestOptions, ToolCall
from lanscoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from lanscoder.tools.types import ToolResult


class AgentLoop:

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        context_builder: ContextBuilder | None = None,
        context_manager: ContextManagerLike | None = None,
        limits: AgentLoopLimits | None = None,
        request_builder: RequestBuilder,
        guardrails: TurnGuardrails,
        observer: TurnObserver,
        mcp_activation: McpActivationTracker,
        tool_executor: ToolExecutor,
        permission_resume: PermissionResumeHandler,
        stream_event_handler: Callable[[ChatStreamEvent], None] | None = None,
        tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None,
        guidance_provider: Callable[[], list[str]] | None = None,
        cancellation_token: CancellationToken | None = None,
        request_options: MainRequestOptions | None = None,
        context_window: int | None = None,
        background_manager: BackgroundJobManager | None = None,
        background_tool_names: frozenset[str] | None = None,
    ) -> None:
        self.session = session
        self.tool_settlement = ToolCallSettlement(session)
        self.task_plan_policy = TaskPlanPolicy(session)
        self.provider = provider
        self.request_options = request_options or MainRequestOptions()
        self.context_window = context_window

        self.context_manager = context_manager
        self.request_builder = request_builder

        self.limits = limits or AgentLoopLimits.default()
        self.max_tool_rounds = self.limits.max_tool_rounds
        self.guardrails = guardrails

        self.last_stream_events: list[ChatStreamEvent] = []
        self.guidance_provider = guidance_provider
        self.cancellation_token = cancellation_token
        self._total_tokens = 0
        self._stream_event_handler = stream_event_handler
        self._tool_event_handler = tool_event_handler

        self.background_manager = background_manager
        self.background_tool_names = background_tool_names if background_tool_names is not None else DEFAULT_BACKGROUND_TOOL_NAMES
        self._task_plan_reconciliation_attempted = False
        self._tool_rounds_completed = 0

        self._mcp_activation = mcp_activation

        self._observer = observer
        self.tool_executor = tool_executor
        self.permission_resume = permission_resume

    @property
    def stream_event_handler(self) -> Callable[[ChatStreamEvent], None] | None:
        return self._stream_event_handler

    @stream_event_handler.setter
    def stream_event_handler(self, value: Callable[[ChatStreamEvent], None] | None) -> None:
        self._stream_event_handler = value
        self._observer.set_stream_event_handler(value)

    @property
    def tool_event_handler(self) -> Callable[[ToolExecutionEvent], None] | None:
        return self._tool_event_handler

    @tool_event_handler.setter
    def tool_event_handler(self, value: Callable[[ToolExecutionEvent], None] | None) -> None:
        self._tool_event_handler = value
        self._observer.set_tool_event_handler(value)

    async def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
        streaming: bool = False,
    ) -> AgentTurnResult:

        if streaming:
            return await self._run_user_turn_streaming(content, attachments=attachments)
        return await self._run_user_turn_async(content, attachments=attachments)

    async def run_nudge_turn(self, *, streaming: bool = False) -> AgentTurnResult:

        if streaming:
            return await self._run_nudge_turn_streaming()
        return await self._run_nudge_turn_async()

    def replace_cancellation_token(self, token: CancellationToken | None) -> None:

        self.cancellation_token = token
        self.tool_executor.cancellation_token = token
        self._observer.replace_cancellation_token(token)

    def clear_stream_events(self) -> None:
        self.last_stream_events = []

    def _run_user_turn_sync(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:

        return asyncio.run(self._run_user_turn_async(content, attachments=attachments))

    async def _run_user_turn_async(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:

        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self.session.append_user_message(content, attachments=attachments)

        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=False),
        )

    def _run_nudge_turn_sync(self) -> AgentTurnResult:

        return asyncio.run(self._run_nudge_turn_async())

    async def _run_nudge_turn_async(self) -> AgentTurnResult:

        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )
        if self.background_manager is None or not self.background_manager.pending_completions(session_id=self.session.session_id):
            return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=None)

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=False),
        )

    async def resume_with_user_input(
        self,
        request_id: str,
        answer: str,
        *,
        streaming: bool = False,
    ) -> AgentTurnResult:

        if streaming:
            return await self._resume_with_user_input_streaming(request_id, answer)
        return await self._resume_with_user_input_async(request_id, answer)

    def _resume_with_user_input_sync(self, request_id: str, answer: str) -> AgentTurnResult:

        return asyncio.run(self._resume_with_user_input_async(request_id, answer))

    async def _resume_with_user_input_async(self, request_id: str, answer: str) -> AgentTurnResult:

        try:
            self.guardrails.check_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self.guardrails.limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self.guardrails.interrupted_response())
        outcome = await self.permission_resume.handle(request_id, answer)
        if outcome.kind != "continue":
            return outcome.result
        self._begin_turn(new_user_turn=False)
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return await self._run_tool_loop(
            partial(self._complete_once_with_recovery, streaming=False),
        )

    async def _resume_with_user_input_streaming(self, request_id: str, answer: str) -> AgentTurnResult:

        try:
            self.guardrails.check_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self.guardrails.limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self.guardrails.interrupted_response())
        outcome = await self.permission_resume.handle(request_id, answer)
        if outcome.kind != "continue":
            return outcome.result
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

    def _record_resumed_tool_round(self) -> None:

        self._tool_rounds_completed += 1

    async def _complete_once(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
        streaming: bool,
    ) -> ChatResponse:
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
                    self._observer.on_stream_event(event)
                    if event.kind == "message_completed":
                        final_response = event.response
                if final_response is None:
                    raise ProviderError(
                        ProviderErrorKind.API_ERROR,
                        "provider stream ended without message_completed event",
                    )
            except ProviderError:
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

    async def _run_tool_loop(self, complete_once, *, initial_tool_choice="auto") -> AgentTurnResult:

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

        return self._complete_turn(response)

    @staticmethod
    def _pending_turn_result(pending_input: UserInputRequest) -> AgentTurnResult:
        return AgentTurnResult(status=AgentTurnStatus.WAITING_FOR_USER_INPUT, pending_input=pending_input)

    def _complete_turn(self, response: ChatResponse) -> AgentTurnResult:
        self.session.append_assistant_response(response)
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

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
        self._observer.on_tool_event(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
                prewrite_review=prewrite_review,
            )
        )

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
        usage = response.usage
        if usage is not None and usage.total_tokens is not None:
            self._total_tokens += usage.total_tokens
        self._observer.on_progress(self.guardrails.call_count, self._total_tokens)

    def usage_summary(self) -> dict[str, int]:
        return self._observer.usage_summary()

    def _compact_if_needed(
        self,
        *,
        trigger: ContextWindowTrigger,
        runtime_instruction: str | None = None,
    ):

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

        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            return []
        definitions = []
        for definition in self.session.tool_registry.definitions():
            if definition.name in HIDDEN_TOOL_STATUS_NAMES:
                continue
            if definition.name in self._mcp_activation.mcp_tool_names and definition.name not in self._mcp_activation.active_names:
                continue
            definitions.append(self._augment_tool_definition(definition))
        return definitions

    def _loop_tool_definitions(self):

        return self._provider_tool_definitions()

    def _augment_tool_definition(self, definition):

        if self.background_manager is None:
            return definition
        if definition.name not in self.background_tool_names:
            return definition
        return with_background_controls(definition)

    def foreground_progress(self) -> dict[str, Any] | None:
        return self._observer.foreground_progress()

    def _begin_turn(self, *, new_user_turn: bool = True) -> None:
        if new_user_turn:
            self._mcp_activation.clear()
            self.guardrails.begin_turn()
            self._task_plan_reconciliation_attempted = False
            self._tool_rounds_completed = 0

    def _append_pending_guidance(self) -> None:
        if self.guidance_provider is None:
            return
        guidance_items = self.guidance_provider()
        for content in guidance_items:
            text = content.strip()
            if text:
                self.session.append_user_message(text)

    def _append_background_notifications(self) -> None:

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

        return self.guardrails.limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT, raw=response.raw)
