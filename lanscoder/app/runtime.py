from __future__ import annotations

from lanscoder.input.attachments import UserAttachment
from lanscoder.utils.text import ellipsis_truncate

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio

from lanscoder.runtime.cancellation import CancellationToken
from lanscoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from lanscoder.agent.background import DEFAULT_BACKGROUND_TOOL_NAMES, BackgroundJobManager
from lanscoder.agent.guardrails import TurnGuardrails
from lanscoder.agent.loop import AgentLoop, ToolExecutionEvent
from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.agent.mcp_activation import McpActivationTracker
from lanscoder.agent.observer import TurnObserver
from lanscoder.agent.permission_resume import CoordinatorPendingStore, PermissionResumeHandler
from lanscoder.agent.request_builder import RequestBuilder
from lanscoder.agent.session import AgentSession
from lanscoder.agent.subagent_engine import DEFAULT_CHILD_LIMITS, SubagentEngine
from lanscoder.agent.tool_execution import ToolExecutor
from lanscoder.agent.user_input import AgentTurnStatus
from lanscoder.runtime.user_input import UserInputRequest
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.permissions.types import PermissionMode
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatResponse, ChatStreamEvent, MainRequestOptions
from lanscoder.tools.background import (
    create_background_cancel_tool,
    create_background_status_tool,
)
from lanscoder.tools.delegate import create_delegate_tool
from lanscoder.tools.types import Tool


def register_loop_tools(
    session,
    *,
    caller_tools,
    background_manager,
    provider,
    request_options,
    subagent_runner=None,
) -> SubagentEngine | None:

    registry = session.tool_registry
    for tool in caller_tools or []:
        if tool.name not in registry.names():
            registry.register(tool)
    if background_manager is not None:
        if "background_status" not in registry.names():
            registry.register(create_background_status_tool(background_manager, session_id=session.session_id))
        if "background_cancel" not in registry.names():
            registry.register(create_background_cancel_tool(background_manager, session_id=session.session_id))
    if subagent_runner is not None and "delegate" not in registry.names():
        registry.register(create_delegate_tool(subagent_runner, parent_session_id=session.session_id))
    return subagent_runner


def create_agent_loop(
    *,
    session,
    provider,
    context_builder=None,
    context_manager=None,
    limits=None,
    request_options=None,
    context_window=None,
    background_manager=None,
    background_tool_names=None,
    guidance_provider=None,
    cancellation_token=None,
    stream_event_handler=None,
    tool_event_handler=None,
    enable_delegate_tool=True,
    **_,
) -> AgentLoop:

    tools = _.pop("tools", None)
    observer = _.pop("observer", None)
    clock = _.pop("clock", None)

    register_loop_tools(
        session,
        caller_tools=tools,
        background_manager=background_manager,
        provider=provider,
        request_options=request_options,
    )

    child_limits = DEFAULT_CHILD_LIMITS

    def _child_runner_factory(*, session, tools, observer, cancellation_token):
        return create_agent_loop(
            session=session,
            provider=provider,
            tools=tools,
            observer=observer,
            cancellation_token=cancellation_token,
            background_manager=None,
            enable_delegate_tool=False,
            limits=child_limits,
            request_options=request_options,
        )

    coordinator = session.permission_coordinator
    project_root = coordinator.permission_manager.policy.project_root if coordinator.permission_manager is not None else None
    engine = SubagentEngine(
        store=session.store,
        provider=provider,
        tools=session.tool_registry.tools(),
        project_root=project_root,
        agents_md=session.agents_md,
        skill_catalog=session.skill_catalog,
        permission_coordinator=coordinator,
        request_options=request_options,
        limits=child_limits,
        background_manager=background_manager,
        child_runner_factory=_child_runner_factory,
    )
    if enable_delegate_tool:
        register_loop_tools(
            session,
            caller_tools=None,
            background_manager=None,
            provider=provider,
            request_options=request_options,
            subagent_runner=engine,
        )

    request_builder = RequestBuilder(
        session=session,
        provider=provider,
        context_builder=context_builder or ContextBuilder(),
        request_options=request_options or MainRequestOptions(),
        context_window=context_window,
    )
    mcp_activation = McpActivationTracker(frozenset(name for name in session.tool_registry.names() if name.startswith("mcp__")))
    if observer is None:
        observer = TurnObserver(
            stream_event_handler=stream_event_handler,
            tool_event_handler=tool_event_handler,
            foreground_progress_provider=lambda: engine.foreground_progress,
        )
    if background_tool_names is None:
        background_tool_names = DEFAULT_BACKGROUND_TOOL_NAMES
    tool_executor = ToolExecutor(
        session=session,
        permission_coordinator=coordinator,
        event_sink=observer,
        cancellation_token=cancellation_token,
        validate_tool_call=mcp_activation.validate,
        observe_tool_result=mcp_activation.observe,
        background_manager=background_manager,
        background_tool_names=background_tool_names,
    )
    guardrails = TurnGuardrails(
        provider=provider,
        limits=limits or AgentLoopLimits.default(),
        clock=clock or time.monotonic,
    )
    permission_resume = PermissionResumeHandler(
        pending_store=CoordinatorPendingStore(coordinator),
        provider=provider,
        tool_executor=tool_executor,
        observer=observer,
        session=session,
        permission_coordinator=coordinator,
        on_tool_round_completed=lambda: None,
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        context_builder=context_builder,
        context_manager=context_manager,
        limits=limits,
        request_options=request_options,
        context_window=context_window,
        request_builder=request_builder,
        guardrails=guardrails,
        observer=observer,
        mcp_activation=mcp_activation,
        tool_executor=tool_executor,
        permission_resume=permission_resume,
        background_manager=background_manager,
        background_tool_names=background_tool_names,
        guidance_provider=guidance_provider,
        cancellation_token=cancellation_token,
        stream_event_handler=stream_event_handler,
        tool_event_handler=tool_event_handler,
    )
    permission_resume.set_tool_round_callback(loop._record_resumed_tool_round)
    return loop


@dataclass(slots=True)
class CurrentSessionState:

    session: AgentSession

    def set_session(self, session: AgentSession) -> None:
        self.session = session

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def runtime_state(self) -> SessionRuntimeState:
        return self.session.runtime_state

    @property
    def current_turn(self) -> int:
        return self.session.current_turn

    def rebuild_view(self) -> SessionView:
        return self.session.rebuild_view()

    @property
    def mode(self) -> str:
        return self.session.permission_mode

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        return self.session.permission_coordinator.set_mode(mode)


@dataclass(slots=True)
class AgentChatRunner:

    current_session: CurrentSessionState
    provider: ChatProvider
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    context_builder: ContextBuilder | None = None
    context_manager: Any | None = None
    limits: AgentLoopLimits | None = None
    use_streaming: bool = False
    request_options: MainRequestOptions = field(default_factory=MainRequestOptions)
    context_window: int | None = None
    loop: AgentLoop | None = None
    request_builder: RequestBuilder | None = None
    loops: list[AgentLoop] = field(default_factory=list)
    last_display_lines: list[str] = field(default_factory=list)
    last_stream_events: list[ChatStreamEvent] = field(default_factory=list)
    last_pending_input: UserInputRequest | None = None
    stream_event_handler: Callable[[ChatStreamEvent], None] | None = None
    tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None
    background_manager: BackgroundJobManager | None = None
    pending_guidance: list[str] = field(default_factory=list)
    _guidance_lock: threading.Lock = field(default_factory=threading.Lock)
    _cancellation_lock: threading.Lock = field(default_factory=threading.Lock)
    _active_cancellation_token: CancellationToken | None = None
    _pending_permission_loop: AgentLoop | None = None

    def set_provider(self, provider: ChatProvider, *, use_streaming: bool) -> None:
        self.set_model(
            provider,
            request_options=MainRequestOptions(),
            context_window=None,
            use_streaming=use_streaming,
        )

    def set_model(
        self,
        provider: ChatProvider,
        *,
        request_options: MainRequestOptions,
        context_window: int | None,
        use_streaming: bool,
    ) -> None:
        self.provider = provider
        self.request_options = request_options
        self.context_window = context_window
        self.use_streaming = use_streaming
        self.last_stream_events = []

    def sync_pending_input_from_current_session(self) -> UserInputRequest | None:
        self.last_pending_input = self.current_session.session.pending_permission_input_request()
        return self.last_pending_input

    def add_guidance(self, content: str) -> None:
        text = content.strip()
        if not text:
            return
        with self._guidance_lock:
            self.pending_guidance.append(text)

    def drain_guidance(self) -> list[str]:
        with self._guidance_lock:
            guidance = list(self.pending_guidance)
            self.pending_guidance.clear()
        return guidance

    def cancel_current_turn(self) -> None:
        with self._cancellation_lock:
            if self._active_cancellation_token is not None:
                self._active_cancellation_token.cancel()

    def _begin_cancellable_turn(self) -> CancellationToken:
        token = CancellationToken()
        with self._cancellation_lock:
            self._active_cancellation_token = token
        return token

    def _finish_cancellable_turn(self, token: CancellationToken) -> None:
        with self._cancellation_lock:
            if self._active_cancellation_token is token:
                self._active_cancellation_token = None

    def _start_turn(self, *, streaming: bool = False) -> tuple[int, CancellationToken, AgentLoop]:
        before_count = len(self.current_session.rebuild_view().messages)
        self.last_pending_input = None
        token = self._begin_cancellable_turn()
        if streaming:
            self.last_display_lines = []
            self.last_stream_events = []
        return before_count, token, self._create_loop(token, streaming=streaming)

    def _resume_turn(self, *, streaming: bool = False) -> tuple[int, CancellationToken, AgentLoop]:
        before_count = len(self.current_session.rebuild_view().messages)
        self.last_pending_input = None
        token = self._begin_cancellable_turn()
        loop = self._pending_permission_loop
        if loop is None or loop.session is not self.current_session.session:
            loop = self._create_loop(token, streaming=streaming)
        else:
            loop.replace_cancellation_token(token)
            loop.stream_event_handler = self.stream_event_handler if streaming else None
            loop.tool_event_handler = self.tool_event_handler
            if streaming:
                self.last_display_lines = []
                self.last_stream_events = []
                loop.clear_stream_events()
        return before_count, token, loop

    def _remember_pending_permission_loop(self, loop: AgentLoop) -> None:
        self._pending_permission_loop = loop if self.current_session.session.pending_permission_execution is not None else None

    def _refresh_turn_output(self, before_count: int, loop: AgentLoop) -> None:
        self.last_stream_events = list(loop.last_stream_events)
        messages = self.current_session.rebuild_view().messages[before_count:]
        self.last_display_lines = _display_lines_from_messages(messages)

    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:

        return asyncio.run(self.arun_user_turn(content, attachments=attachments))

    def resume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:

        return asyncio.run(self.aresume_with_user_input(request_id, answer))

    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:

        before_count, cancellation_token, loop = self._start_turn(streaming=self.use_streaming)
        try:
            result = await anyio.to_thread.run_sync(
                _run_coroutine_in_thread,
                loop.run_user_turn(
                    content,
                    attachments=attachments,
                    streaming=self.use_streaming,
                ),
            )
        finally:
            self._finish_cancellable_turn(cancellation_token)
        return self._finish_agent_result(before_count, loop, result)

    async def anudge_turn(self) -> ChatResponse:

        before_count, cancellation_token, loop = self._start_turn(streaming=self.use_streaming)
        try:
            result = await anyio.to_thread.run_sync(
                _run_coroutine_in_thread,
                loop.run_nudge_turn(streaming=self.use_streaming),
            )
        finally:
            self._finish_cancellable_turn(cancellation_token)
        if result.response is None:
            return ChatResponse(provider=self.provider.name, model=self.provider.model, content="")
        return self._finish_agent_result(before_count, loop, result)

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        before_count, cancellation_token, loop = self._resume_turn(streaming=self.use_streaming)
        try:
            result = await anyio.to_thread.run_sync(
                _run_coroutine_in_thread,
                loop.resume_with_user_input(
                    request_id,
                    answer,
                    streaming=self.use_streaming,
                ),
            )
        finally:
            self._finish_cancellable_turn(cancellation_token)
        return self._finish_agent_result(before_count, loop, result)

    def _finish_agent_result(self, before_count: int, loop: AgentLoop, result) -> ChatResponse:
        self.last_pending_input = result.pending_input
        self._remember_pending_permission_loop(loop)
        self._refresh_turn_output(before_count, loop)
        if result.response is not None:
            if result.response.content and not self.last_display_lines:
                self.last_display_lines.append(result.response.content)
            return result.response
        return self._waiting_for_input_response(result.pending_input)

    def _current_tools(self) -> list[Tool] | None:

        return self.tools_provider() if self.tools_provider is not None else self.tools

    def context_budget(self, view):
        builder = self.request_builder
        if builder is None:
            builder = RequestBuilder(
                session=self.current_session.session,
                provider=self.provider,
                context_builder=self.context_builder or ContextBuilder(),
                request_options=self.request_options or MainRequestOptions(),
                context_window=self.context_window,
            )
            self.request_builder = builder
        definitions = self.loop._loop_tool_definitions() if self.loop is not None else self._registry_tool_definitions()
        return builder.context_budget_for_view(view, runtime_instruction=None, definitions=definitions)

    def _registry_tool_definitions(self):

        return [d for d in self.current_session.session.tool_registry.definitions() if d.name not in HIDDEN_TOOL_STATUS_NAMES]

    def _create_loop(self, cancellation_token: CancellationToken, *, streaming: bool = False) -> AgentLoop:
        kwargs = {
            "session": self.current_session.session,
            "provider": self.provider,
            "request_options": self.request_options,
            "context_window": self.context_window,
            "tools": self._current_tools(),
            "context_builder": self.context_builder,
            "context_manager": self.context_manager,
            "limits": self.limits,
            "tool_event_handler": self.tool_event_handler,
            "guidance_provider": self.drain_guidance,
            "cancellation_token": cancellation_token,
            "background_manager": self.background_manager,
        }
        if streaming:
            kwargs["stream_event_handler"] = self.stream_event_handler
        loop = create_agent_loop(**kwargs)
        self.loop = loop
        self.request_builder = loop.request_builder
        self.loops.append(loop)
        return loop

    def foreground_subagent(self) -> dict[str, Any] | None:
        for loop in self.loops:
            info = loop.foreground_progress()
            if info is not None:
                return info
        return None

    def _waiting_for_input_response(self, pending: UserInputRequest | None) -> ChatResponse:
        response = ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=pending.question if pending else "等待用户输入。",
            finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
            raw={"pending_input": pending},
        )
        if response.content:
            self.last_display_lines.append(response.content)
        return response


def _display_lines_from_messages(messages: list[AgentMessage]) -> list[str]:

    lines: list[str] = []
    for message in messages:
        if message.role == "assistant":
            lines.extend(_assistant_lines(message.parts))
        elif message.role == "tool":
            lines.extend(_tool_lines(message.parts))
    return lines


def _run_coroutine_in_thread(coro):
    return asyncio.run(coro)


def _assistant_lines(parts: list[MessagePart]) -> list[str]:
    lines: list[str] = []
    for part in parts:
        if part.kind == "text" and part.content:
            lines.append(part.content)
        elif part.kind == "tool_call":
            metadata = part.metadata
            name = str(metadata.get("tool_name") or "tool")
            if name in HIDDEN_TOOL_STATUS_NAMES:
                continue
            arguments = json.dumps(metadata.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
            lines.append(f"Tool call: {name} {ellipsis_truncate(arguments, 400, normalize_ws=True)}")
    return lines


def _tool_lines(parts: list[MessagePart]) -> list[str]:
    lines: list[str] = []
    for part in parts:
        if part.kind != "tool_result":
            continue
        metadata = part.metadata
        name = str(metadata.get("tool_name") or "tool")
        if name in HIDDEN_TOOL_STATUS_NAMES:
            continue
        status = "success" if metadata.get("ok", True) else "failed"
        content = ellipsis_truncate(part.content, 400, normalize_ws=True)
        lines.append(f"Tool result: {name} {status}: {content}")
    return lines
