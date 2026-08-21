"""应用层运行时代理:持有当前会话,驱动 AgentLoop 执行/恢复回合,并缓存回合输出供 TUI 展示。"""

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

from lanscoder.utils.cancellation import CancellationToken
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
from lanscoder.permissions.user_input import UserInputRequest
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
    """向会话工具注册表补齐循环所需工具:调用者工具、后台状态/取消、delegate 子代理工具。"""

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
    """装配一次 AgentLoop:注册工具、构造子代理引擎、请求构建器、观察者、工具执行器与权限恢复器。"""

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
    """对当前 AgentSession 的薄封装,支持会话热切换。"""

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
        """把权限模式设置委托给会话的权限协调器。"""
        return self.session.permission_coordinator.set_mode(mode)


@dataclass(slots=True)
class AgentChatRunner:
    """应用层运行时代理:驱动 AgentLoop 执行/恢复回合,缓存回合输出与挂起输入供 UI 使用。"""

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
    # 本回合按序的 (reasoning 文本, 秒数, 消息是否以 tool_call 收尾)；
    # 供 TUI 收尾 reconcile 把 store 里的时长回填到 live thinking 子行。
    # 合并边界是 tool_call 而非 text:replay/live 的 append_thinking 只查末位
    # child 是否为 THINKING,text 结束结算后仍是末位,仅 tool_call 追加 TOOL
    # child 顶掉末位、切断下一条 reasoning 的合并链。
    _turn_reasonings: list[tuple[str, float | None, bool]] = field(default_factory=list)

    def set_provider(self, provider: ChatProvider, *, use_streaming: bool) -> None:
        """用默认请求选项替换 provider 并设置流式开关。"""
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
        """替换 provider 并刷新请求选项、上下文窗口与流式开关。"""
        self.provider = provider
        self.request_options = request_options
        self.context_window = context_window
        self.use_streaming = use_streaming
        self.last_stream_events = []

    def sync_pending_input_from_current_session(self) -> UserInputRequest | None:
        """从当前会话同步挂起的输入请求。"""
        self.last_pending_input = self.current_session.session.pending_permission_input_request()
        return self.last_pending_input

    def add_guidance(self, content: str) -> None:
        """线程安全地追加一条运行指引。"""
        text = content.strip()
        if not text:
            return
        with self._guidance_lock:
            self.pending_guidance.append(text)

    def drain_guidance(self) -> list[str]:
        """取走并清空所有待发送的运行指引。"""
        with self._guidance_lock:
            guidance = list(self.pending_guidance)
            self.pending_guidance.clear()
        return guidance

    def cancel_current_turn(self) -> None:
        """取消当前活跃回合的取消令牌。"""
        with self._cancellation_lock:
            if self._active_cancellation_token is not None:
                self._active_cancellation_token.cancel()

    def _begin_cancellable_turn(self) -> CancellationToken:
        """分配并登记新的可取消回合令牌。"""
        token = CancellationToken()
        with self._cancellation_lock:
            self._active_cancellation_token = token
        return token

    def _finish_cancellable_turn(self, token: CancellationToken) -> None:
        """回合结束时注销令牌(仅当它仍是最新令牌)。"""
        with self._cancellation_lock:
            if self._active_cancellation_token is token:
                self._active_cancellation_token = None

    def _start_turn(self, *, streaming: bool = False) -> tuple[int, CancellationToken, AgentLoop]:
        """开始一次新回合:记录起始消息数、重置输出缓冲、创建 AgentLoop。"""
        self._turn_reasonings.clear()
        before_count = len(self.current_session.rebuild_view().messages)
        self.last_pending_input = None
        token = self._begin_cancellable_turn()
        if streaming:
            self.last_display_lines = []
            self.last_stream_events = []
        return before_count, token, self._create_loop(token, streaming=streaming)

    def _resume_turn(self, *, streaming: bool = False) -> tuple[int, CancellationToken, AgentLoop]:
        """恢复回合:复用挂起的 AgentLoop 或新建,替换取消令牌。"""
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
        """仅在存在挂起权限执行时记住当前 loop,供恢复回合复用。"""
        self._pending_permission_loop = loop if self.current_session.session.pending_permission_execution is not None else None

    def _refresh_turn_output(self, before_count: int, loop: AgentLoop) -> None:
        """用本回合新增消息刷新展示行与流事件缓冲。"""
        self.last_stream_events = list(loop.last_stream_events)
        messages = self.current_session.rebuild_view().messages[before_count:]
        self.last_display_lines = _display_lines_from_messages(messages)
        self._accumulate_turn_reasonings(before_count)

    def _accumulate_turn_reasonings(self, before_count: int) -> None:
        """把边界后新增 assistant 消息的 reasoning 摘要收进本回合窗口。"""
        messages = self.current_session.rebuild_view().messages[before_count:]
        for message in messages:
            if getattr(message, "role", None) != "assistant":
                continue
            reasoning, seconds = _reasoning_entry(message)
            if not reasoning and seconds is None:
                continue
            # 合并边界:仅 tool_call part 切断合并链(text part 结算后 THINKING
            # child 仍在块末尾,下一条 reasoning 会继续合并进它)。
            ended_with_tool = any(part.kind == "tool_call" for part in getattr(message, "parts", []) or [])
            self._turn_reasonings.append((reasoning, seconds, ended_with_tool))

    @property
    def last_turn_reasonings(self) -> list[tuple[str, float | None, bool]]:
        """本回合按序的 reasoning 条目副本,供 TUI 只读消费。"""
        return list(self._turn_reasonings)

    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """同步入口:在新事件循环中运行一次用户回合。"""

        return asyncio.run(self.arun_user_turn(content, attachments=attachments))

    def resume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        """同步入口:携带用户输入恢复被挂起的回合。"""

        return asyncio.run(self.aresume_with_user_input(request_id, answer))

    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """异步执行一次用户回合,在线程中运行 loop 并返回响应。"""

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
        """异步执行一次引导回合,用于处理后台任务完成。"""

        before_count, cancellation_token, loop = self._start_turn(streaming=self.use_streaming)
        try:
            result = await anyio.to_thread.run_sync(
                _run_coroutine_in_thread,
                loop.run_nudge_turn(streaming=self.use_streaming),
            )
        finally:
            self._finish_cancellable_turn(cancellation_token)
        if result.response is None:
            self._accumulate_turn_reasonings(before_count)
            return ChatResponse(provider=self.provider.name, model=self.provider.model, content="")
        return self._finish_agent_result(before_count, loop, result)

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        """异步恢复被挂起的回合并返回响应。"""
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
        """回合收尾:同步挂起输入、刷新输出,返回响应或等待输入的占位响应。"""
        self.last_pending_input = result.pending_input
        self._remember_pending_permission_loop(loop)
        self._refresh_turn_output(before_count, loop)
        if result.response is not None:
            if result.response.content and not self.last_display_lines:
                self.last_display_lines.append(result.response.content)
            return result.response
        return self._waiting_for_input_response(result.pending_input)

    def _current_tools(self) -> list[Tool] | None:
        """返回当前生效的工具集(优先走 tools_provider 以支持热更新)。"""

        return self.tools_provider() if self.tools_provider is not None else self.tools

    def context_budget(self, view):
        """按当前视图与工具定义计算上下文预算。"""
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
        """返回剔除隐藏工具后的注册表工具定义。"""

        return [d for d in self.current_session.session.tool_registry.definitions() if d.name not in HIDDEN_TOOL_STATUS_NAMES]

    def _create_loop(self, cancellation_token: CancellationToken, *, streaming: bool = False) -> AgentLoop:
        """构建并缓存 AgentLoop,记录到 loops 列表供前台子代理查询。"""
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
        """返回第一个有前台进度的 loop 的信息,供 TUI 展示。"""
        for loop in self.loops:
            info = loop.foreground_progress()
            if info is not None:
                return info
        return None

    def flush_background_notifications(self) -> None:
        """把当前会话所有已完成的后台通知落盘(供退出前调用)。"""
        session = self.current_session.session
        for loop in self.loops:
            if getattr(loop, "session", None) is session:
                loop.flush_background_notifications()

    def _waiting_for_input_response(self, pending: UserInputRequest | None) -> ChatResponse:
        """构造等待用户输入时的占位响应。"""
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
    """把新增消息展开为 assistant/tool 的展示行。"""

    lines: list[str] = []
    for message in messages:
        if message.role == "assistant":
            lines.extend(_assistant_lines(message.parts))
        elif message.role == "tool":
            lines.extend(_tool_lines(message.parts))
    return lines


def _reasoning_entry(message) -> tuple[str, float | None]:
    """从 assistant 消息元数据读出 reasoning 文本与耗时;缺省时返回空。"""

    metadata = getattr(message, "metadata", None) or {}
    diagnostics = metadata.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        return "", None
    return str(diagnostics.get("reasoning") or ""), diagnostics.get("reasoning_seconds") or None


def _run_coroutine_in_thread(coro):
    """在新事件循环里运行协程,用于跨线程桥接。"""
    return asyncio.run(coro)


def _assistant_lines(parts: list[MessagePart]) -> list[str]:
    """把 assistant 消息的文本与工具调用展开为展示行(隐藏工具除外)。"""
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
    """把工具结果消息展开为展示行(隐藏工具除外)。"""
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
