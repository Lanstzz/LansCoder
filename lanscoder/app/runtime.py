"""TUI 运行期 session 状态和聊天入口。

Textual widget 只负责显示和输入；这里把“当前 session 可被 resume 替换”和“普通输入
调用 AgentLoop”封成很薄的一层，避免 UI 直接持有 agent 编排细节。
"""

# ============================================================================
# 阅读路径导航 (Reading Path Guide)
# ============================================================================
# 这是 UI 层 (TUI/CLI) 和 Agent 层之间的薄代理层 (thin proxy layer)。
# 两个核心类：
#   CurrentSessionState — 可替换的当前 session 代理，/resume 命令通过 set_session()
#                         替换内部 session，所有命令处理器自动看到新会话
#   AgentChatRunner     — 聊天入口，把用户消息交给 AgentLoop 执行
# 关键调用链：
#   UI → AgentChatRunner.run_user_turn() → AgentLoop.run_user_turn() → provider
# → 上一步阅读：lanscoder/app/factory.py (create_lanscoder_app 中创建 AgentChatRunner)
# → 下一步阅读：lanscoder/agent/loop.py (AgentLoop)
# ============================================================================

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
from lanscoder.agent.permission_resume import PermissionResumeHandler
from lanscoder.agent.request_builder import RequestBuilder
from lanscoder.agent.session import AgentSession, SessionPendingStore
from lanscoder.agent.subagent_engine import SubagentEngine
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


# ----------------------------------------------------------------------------
# 组装根 (assembly root)：create_agent_loop / register_loop_tools
# ----------------------------------------------------------------------------
# AgentLoop 的所有协作对象（request_builder / guardrails / observer / mcp_activation /
# tool_executor / SubagentEngine / delegate tool）都在这里构造并注入，AgentLoop 构造不再
# 带任何兜底。child_runner_factory 把子 agent 的 child session 交给同一组装根，因此
# SubagentEngine 不需要 import lanscoder.agent.loop（打破历史 import 环）。
def register_loop_tools(
    session,
    *,
    caller_tools,
    background_manager,
    provider,
    request_options,
    subagent_runner=None,
) -> SubagentEngine | None:
    """在组装根注册 caller / 后台控制工具；subagent_runner 非空时补注册 delegate。

    ``AgentLoop`` 构造时不再注册任何工具，registry 是唯一的工具来源。同名工具跳过注册
    （去重）；delegate 只在 ``subagent_runner`` 显式传入时注册（由组装根传入已构造的
    SubagentEngine），其 child 工具快照不含 delegate，防止递归委托。
    """

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
    """全协作对象构造注入：AgentLoop 的唯一构造路径。

    ``tools``（仅用于注册，不回传 loop）与可选注入的 ``observer``/``clock`` 经
    ``**_`` 捕获。子 loop（child_runner_factory 路径）显式传入 observer，组装根直接
    复用而不新建，保证子 agent 的进度仍按 SubagentEngine 的分流走。
    """

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

    def _child_runner_factory(*, session, tools, observer, cancellation_token):
        return create_agent_loop(
            session=session,
            provider=provider,
            tools=tools,
            observer=observer,
            cancellation_token=cancellation_token,
            background_manager=None,
            enable_delegate_tool=False,
            limits=limits,
            request_options=request_options,
        )

    project_root = session.permission_manager.policy.project_root if session.permission_manager is not None else None
    engine = SubagentEngine(
        store=session.store,
        provider=provider,
        tools=session.tool_registry.tools(),
        project_root=project_root,
        agents_md=session.agents_md,
        skill_catalog=session.skill_catalog,
        permission_manager=session.permission_manager,
        sandbox_access=session.sandbox_access,
        request_options=request_options,
        limits=limits,
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
        pending_store=SessionPendingStore(session),
        provider=provider,
        tool_executor=tool_executor,
        observer=observer,
        session=session,
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
    # handler 先以 no-op 构造（此时 loop 尚不存在），loop 构造完成后把工具轮次
    # 计数回调绑到 loop 私有方法，避免 handler 反向依赖 loop。
    permission_resume.set_tool_round_callback(loop._record_resumed_tool_round)
    return loop


# ----------------------------------------------------------------------------
# "可替换代理"模式 (swappable proxy pattern)
# ----------------------------------------------------------------------------
# 为什么不直接把 AgentSession 传给 ContextCommandHandler / UI widget？
# 因为 /resume 命令需要在运行时热替换当前会话：用户选中一个历史 session，
# 把整个应用切到那个 session 上继续工作。如果各处都直接持有 AgentSession 引用，
# 替换就必须通知每一个持有者。通过 CurrentSessionState 这一层间接，
# 只需调用一次 set_session()，所有通过代理访问 session 的组件自动看到新会话。
# ----------------------------------------------------------------------------
@dataclass(slots=True)
class CurrentSessionState:
    """可替换的当前 session 代理。

    `ContextCommandHandler` 只需要 `session_id`、`runtime_state`、`current_turn` 和
    `rebuild_view()`；把这些属性代理出来后，`/resume` 只要替换内部 session，context
    命令自然会看见新会话。
    """

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
        return self.session.mode

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        return self.session.set_permission_mode(mode)


# ----------------------------------------------------------------------------
# 用户 turn 的统一入口 (unified entry for user turns)
# ----------------------------------------------------------------------------
# 所有用户消息——普通聊天、权限确认恢复——都经过这个类。它负责：
#   1. 组装一次 AgentLoop 所需的依赖（session, provider, tools, limits, ...）
#   2. 通过 _start_turn() / _resume_turn() 开启或恢复一轮
#   3. 把 AgentTurnResult 整理成 UI 可消费的 ChatResponse
# 关键字段：
#   current_session      — 可替换代理，/resume 时 set_session() 替换内部 session
#   provider             — ChatProvider 实例，来自 factory.py::create_provider_for_model()
#   tools / tools_provider
#                        — 静态工具列表，或每次 turn 动态解析工具的 callable
#   limits               — AgentLoopLimits：tool 轮次/超时/provider 调用上限
#   use_streaming        — 是否使用 provider 的 streaming 响应
#   last_pending_input   — 上一次暂停留下的权限确认请求 (UserInputRequest)
#   _pending_permission_loop
#                        — 暂停时保留的 AgentLoop，权限恢复时复用而不是新建
# ----------------------------------------------------------------------------
@dataclass(slots=True)
class AgentChatRunner:
    """普通聊天入口，把当前 session 交给 AgentLoop 执行一轮。"""

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
        """把一条程序化「运行时指令」塞进待注入队列，下一轮作为 user 消息追加。

        NOTE: 当前生产代码尚未调用此方法（只有测试在用），是一条预留扩展点——设计上供
        hook / 自动化 / 外部触发在运行中给 agent 递一句指令（例如「你现在先做 X」）。
        注入走 append_user_message，因此目前仍算作真实用户轮次（会进 /recall、占 turn）。
        """
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

    # ------------------------------------------------------------------
    # _start_turn: 开启一轮新聊天，创建全新 AgentLoop。
    # _resume_turn: 恢复暂停的权限确认，复用 _pending_permission_loop 以保留
    #               AgentLoop 内部的 budget/state，只替换 cancellation token 和 handlers。
    # ------------------------------------------------------------------
    def _start_turn(self, *, streaming: bool = False) -> tuple[int, CancellationToken, AgentLoop]:
        # before_count 记录本轮开始前的消息数，结束后用它切片出"本轮新增消息"
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
            # Permission resume reuses the paused AgentLoop so budget/state continue.
            # TUI/runtime may have installed fresher stream/tool handlers (and a new
            # turn token) while waiting for the user; rebind them or live UI events
            # keep going through the pre-pause closures and get dropped as stale.
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

    # ------------------------------------------------------------------
    # 同步外层入口 (sync outer boundary)
    # ------------------------------------------------------------------
    # CLI 调用者使用。内部通过 asyncio.run() 桥接到异步版本 arun_user_turn()。
    # Textual 已经运行在 asyncio event loop 里，所以 UI 侧应直接 await arun_user_turn()，
    # 不要再走这一层。
    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """Synchronous outer boundary for CLI callers."""

        return asyncio.run(self.arun_user_turn(content, attachments=attachments))

    # ------------------------------------------------------------------
    # 权限确认恢复入口 (resume after permission prompt)
    # ------------------------------------------------------------------
    # 为什么需要这个独立入口，而不是把回答当作下一条用户消息？
    # 因为权限确认时 AgentLoop 已经发出了一个 tool_call，正在等待对应的 tool_result。
    # 必须用本地存储的原始 tool_call（request_id）恢复，不能让模型重发。
    # 委托给异步版本 aresume_with_user_input()，参见 AgentLoop.resume_with_user_input()
    # (lanscoder/agent/loop.py)。
    def resume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        """恢复等待中的权限确认。

        普通 `ask_user` 后续仍走新的用户消息；权限确认必须先补齐原 tool_call 的
        tool_result，所以 UI 通过这个入口把用户选择交回 agent loop。
        """

        return asyncio.run(self.aresume_with_user_input(request_id, answer))

    # ------------------------------------------------------------------
    # 异步聊天入口 (async chat entry)
    # ------------------------------------------------------------------
    # 流程：
    #   _start_turn()          → 创建新 AgentLoop + CancellationToken，记录当前消息数
    #   loop.run_user_turn()   → 实际跑一轮 agent loop（见 lanscoder/agent/loop.py）
    #   _finish_agent_result() → 把 AgentTurnResult 转成 ChatResponse，保存显示行
    # 用 anyio.to_thread.run_sync + _run_coroutine_in_thread 把协程丢到独立线程运行，
    # 这样 Textual 的 event loop 不会被阻塞，同时 cancellation 可以从外部触发。
    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """异步聊天入口。

        Textual 已经运行在 asyncio event loop 中，所以 UI 需要 await 这个入口；只有这里
        才会在 `use_streaming=True` 时消费 provider 的内部 stream event。
        """

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
        """运行一个不带用户输入的唤醒轮次，投递待处理的后台完成通知。

        用于子 agent 完成后唤醒主 agent 汇报结果。与 arun_user_turn 的区别是
        不写 user_message、不递增 current_turn；通知内容由 AgentLoop 内部
        _append_background_notifications 投影成 provider 的 user 消息。
        """

        before_count, cancellation_token, loop = self._start_turn(streaming=self.use_streaming)
        try:
            result = await anyio.to_thread.run_sync(
                _run_coroutine_in_thread,
                loop.run_nudge_turn(streaming=self.use_streaming),
            )
        finally:
            self._finish_cancellable_turn(cancellation_token)
        if result.response is None:
            # 无待投递通知：不产生任何输出。
            return ChatResponse(provider=self.provider.name, model=self.provider.model, content="")
        return self._finish_agent_result(before_count, loop, result)

    # 异步权限恢复入口，流程同 arun_user_turn()，但走 _resume_turn() 复用
    # 暂停的 AgentLoop，调用 AgentLoop.resume_with_user_input()。
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

    # ------------------------------------------------------------------
    # 收尾一轮 turn：把 AgentTurnResult 转成 ChatResponse。
    #   - 保存 pending_input（如果有权限确认等待中）
    #   - 记住暂停的 AgentLoop，供下次 resume 复用
    #   - 从 loop 的 stream events + 新增消息生成 last_display_lines 给 TUI 显示
    #   - 如果 result.response 为空，回退到"等待用户输入"的占位响应
    # ------------------------------------------------------------------
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
        """Resolve tools once per loop so the session registry sees that same list."""

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
        """从 session 工具注册表取可见 schema（无 loop 的 augment/MCP 激活过滤）。

        MCP 激活过滤（只暴露已激活的 mcp__ 工具）在缝 6 引入 McpActivationTracker 后由
        组装根接入，runtime 不需要再造一份过滤逻辑。
        """

        return [d for d in self.current_session.session.tool_registry.definitions() if d.name not in HIDDEN_TOOL_STATUS_NAMES]

    # ------------------------------------------------------------------
    # AgentLoop 工厂 (per-turn loop factory)
    # ------------------------------------------------------------------
    # 每次 turn 创建一个新的 AgentLoop 实例，把 runner 上攒的所有依赖
    # （session, provider, tools, limits, handlers, cancellation_token, ...）
    # 传递过去。这是 factory 组装的 runner 状态 → AgentLoop 运行期状态的传递点。
    # 创建的 loop 追加到 self.loops，便于调试/事后查看历史。
    def _create_loop(self, cancellation_token: CancellationToken, *, streaming: bool = False) -> AgentLoop:
        # 组装 AgentLoop 构造参数；streaming=False 时不传 stream handler，
        # 避免 AgentLoop 内部做无用的 stream 分发。
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
        """当前前台 delegate 子 agent 的实时进度（无则 None），供 TUI 渲染。"""
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
    """把一轮新增事实压成 TUI 可读的短行。

    这里不重新编排 agent，只读取本轮已经落到 event log 的消息。这样 TUI 可以看到
    tool call/result 摘要，又不会知道 provider/tool 协议细节。
    """

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
