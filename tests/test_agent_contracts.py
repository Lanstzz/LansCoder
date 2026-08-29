"""Loop 协作对象契约测试：签名固定 + 纯变换行为（零 loop 状态）。"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lanscoder.core.runtime import AgentChatRunner, create_agent_loop, register_loop_tools
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.guardrails import TurnGuardrails
from lanscoder.agent.loop import AgentLoop
from lanscoder.agent.loop_limits import _AgentLoopLimitReached, AgentLoopLimits
from lanscoder.agent.mcp_activation import McpActivationTracker
from lanscoder.agent.permission import PermissionCoordinator
from lanscoder.agent.permission_resume import CoordinatorPendingStore
from lanscoder.agent.ports import SessionTurnRunner
from lanscoder.agent.request_builder import PreparedMainRequest, RequestBuilder
from lanscoder.agent.session import AgentSession
from lanscoder.agent.subagent_engine import SubagentEngine
from lanscoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from lanscoder.agent.user_input import AgentTurnResult, AgentTurnStatus
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.context.store import JsonlSessionStore
from lanscoder.mcp.adapter import adapt_mcp_tool
from lanscoder.mcp.models import McpToolDescription
from lanscoder.mcp.search import McpSearchEntry, create_mcp_tool_search
from lanscoder.permissions.types import PermissionMode
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    MainRequestOptions,
    ProviderCapabilities,
    ToolCall,
    ToolDefinition,
)
from lanscoder.subagent.types import SubagentRunner
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.tools.types import Tool, ToolResult, make_text_result
from lanscoder.tools.write import create_write_tool

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeProvider(ChatProvider):
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("contract test should not call provider")


def test_request_builder_build_contract_signature() -> None:
    params = list(inspect.signature(RequestBuilder.build).parameters)
    assert params[0] == "self" and params[1] == "view" and "definitions" in params
    assert inspect.signature(RequestBuilder.build).parameters["definitions"].kind == inspect.Parameter.KEYWORD_ONLY
    bparams = list(inspect.signature(RequestBuilder.context_budget_for_view).parameters)
    assert bparams[1] == "view" and "runtime_instruction" in bparams and "definitions" in bparams


def test_request_builder_build_pure_behavior(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_contract", agents_md="")
    session.append_user_message("hello")
    context_builder = ContextBuilder()
    builder = RequestBuilder(
        session=session,
        provider=FakeProvider(),
        context_builder=context_builder,
        request_options=MainRequestOptions(),
        context_window=32768,
    )
    view = session.rebuild_view()
    d1 = ToolDefinition(name="echo", description="回显文本", parameters={})

    prepared = builder.build(view, definitions=[d1])

    assert isinstance(prepared, PreparedMainRequest)
    assert prepared.request.messages[0].role == "system"
    assert any(message.role == "user" and "hello" in message.content for message in prepared.request.messages)
    assert prepared.request.tools == [d1]
    assert prepared.request.tool_choice == "auto"
    assert prepared.tool_result_part_ids == context_builder.projected_tool_result_part_ids(view)
    assert prepared.request_id
    assert prepared.projection_fingerprint


def test_turn_guardrails_contract_signature() -> None:
    for name in ("reserve_call", "check_timeout", "begin_turn"):
        assert list(inspect.signature(getattr(TurnGuardrails, name)).parameters) == ["self"]
    assert "reason" in inspect.signature(TurnGuardrails.limit_response).parameters
    guardrails = TurnGuardrails(provider=FakeProvider(), limits=AgentLoopLimits.default())
    assert isinstance(guardrails.call_count, int)


def test_turn_guardrails_behavior() -> None:
    clock_now = {"value": 100.0}

    def clock() -> float:
        return clock_now["value"]

    guardrails = TurnGuardrails(
        provider=FakeProvider(),
        limits=AgentLoopLimits(max_provider_calls=1, max_turn_seconds=5),
        clock=clock,
    )
    guardrails.begin_turn()
    guardrails.reserve_call()
    assert guardrails.call_count == 1
    with pytest.raises(_AgentLoopLimitReached):
        guardrails.reserve_call()
    guardrails.begin_turn()
    assert guardrails.call_count == 0
    clock_now["value"] = 110.0
    with pytest.raises(_AgentLoopLimitReached):
        guardrails.check_timeout()


def test_registry_complete_before_loop(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_registry_single_source", agents_md="")
    provider = FakeProvider()
    manager = BackgroundJobManager()
    caller_tool = Tool(
        definition=ToolDefinition(
            name="caller_tool",
            description="a caller-supplied tool",
            parameters={"type": "object", "properties": {}},
        ),
        executor=lambda: make_text_result("caller_tool", "ok"),
    )
    try:
        register_loop_tools(
            session,
            caller_tools=[caller_tool],
            background_manager=manager,
            provider=provider,
            request_options=MainRequestOptions(),
        )

        # 一阶段只注册 caller + 后台控制；delegate 由 create_agent_loop 二阶段（传入
        # SubagentEngine）补注册，构造 AgentLoop 之前 registry 已是单一工具源。
        names = session.tool_registry.names()
        for expected in ("caller_tool", "background_status", "background_cancel"):
            assert expected in names
        assert "delegate" not in names

        # 工具注册完全上移到组装根：make_loop 经 create_agent_loop 只补 delegate，
        # AgentLoop 构造本身 register 零新增。
        original_register = session.tool_registry.register
        register_calls: list[str] = []

        def _spy_register(tool):
            register_calls.append(tool.name)
            return original_register(tool)

        session.tool_registry.register = _spy_register
        make_loop(session=session, provider=provider)
        assert register_calls == ["delegate"]
        assert "delegate" in session.tool_registry.names()
    finally:
        manager.shutdown()


@dataclass
class _SequenceProvider(ChatProvider):
    """Records requests and replays a fixed response queue for both complete/astream."""

    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake-seq"

    @property
    def model(self) -> str:
        return "fake-seq-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        response = self.responses.pop(0)
        yield ChatStreamEvent(kind="message_started")
        for tool_call in response.tool_calls:
            yield ChatStreamEvent(kind="tool_call_started", tool_call_id=tool_call.id, tool_name=tool_call.name)
            yield ChatStreamEvent(kind="tool_call_completed", tool_call=tool_call)
        if response.content:
            yield ChatStreamEvent(kind="text_delta", text=response.content)
        yield ChatStreamEvent(kind="message_completed", response=response)


class _RecordingObserver:
    """Injected observer that records every dispatch without forwarding."""

    def __init__(self) -> None:
        self.progress: list[tuple[int, int]] = []
        self.tool_events: list[ToolExecutionEvent] = []
        self.stream_events: list[ChatStreamEvent] = []
        self._provider_calls = 0
        self._total_tokens = 0
        self._stream_event_handler = None
        self._tool_event_handler = None

    def on_turn_started(self) -> None:
        pass

    def on_progress(self, provider_calls: int, total_tokens: int) -> None:
        self.progress.append((provider_calls, total_tokens))
        self._provider_calls = provider_calls
        self._total_tokens = total_tokens

    def on_tool_event(self, event: ToolExecutionEvent) -> None:
        self.tool_events.append(event)

    def on_stream_event(self, event: ChatStreamEvent) -> None:
        self.stream_events.append(event)

    def foreground_progress(self) -> dict[str, object] | None:
        return None

    def usage_summary(self) -> dict[str, int]:
        return {"provider_calls": self._provider_calls, "total_tokens": self._total_tokens}

    def set_stream_event_handler(self, handler) -> None:
        self._stream_event_handler = handler

    def set_tool_event_handler(self, handler) -> None:
        self._tool_event_handler = handler

    def replace_cancellation_token(self, token) -> None:
        pass


class _ContractMcpCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> object:
        self.calls.append((server, tool, arguments))
        return {"content": [{"type": "text", "text": "issue result"}]}


def _contract_mcp_tools(caller: _ContractMcpCaller) -> list[Tool]:
    issue = adapt_mcp_tool(
        caller,
        "github",
        McpToolDescription("get_issue", "Read one issue.", {"type": "object", "properties": {}}),
    )
    create = adapt_mcp_tool(
        caller,
        "github",
        McpToolDescription("create_issue", "Create an issue.", {"type": "object", "properties": {}}),
    )
    search = create_mcp_tool_search(
        (
            McpSearchEntry("github", "get_issue", issue.definition),
            McpSearchEntry("github", "create_issue", create.definition),
        )
    )
    return [issue, create, search]


def _echo_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(name="echo", description="回显文本", parameters={}),
        executor=lambda **kw: make_text_result("echo", "ok"),
    )


def test_observer_consumer_pipeline(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_observer_pipeline",
        agents_md="",
        tools=[_echo_tool()],
    )
    session.permission_coordinator.set_mode(PermissionMode.BYPASS)
    provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-seq", model="fake-seq-model", content="done"),
        ]
    )
    observer = _RecordingObserver()
    loop = make_loop(session=session, provider=provider, observer=observer)

    result = loop._run_user_turn_sync("echo")

    assert result.content == "done"
    assert observer.progress
    provider_calls, total_tokens = observer.progress[-1]
    assert provider_calls >= 1
    assert total_tokens >= 0
    valid_kinds = {"prewrite_review", "started", "finished", "permission_requested", "denied", "interrupted", "background_started"}
    assert any(isinstance(event, ToolExecutionEvent) and event.kind in valid_kinds for event in observer.tool_events)
    assert loop.usage_summary() == {"provider_calls": provider_calls, "total_tokens": total_tokens}

    # streaming 模式：同一 observer 也收到 ChatStreamEvent（非流式 turn 不产生）。
    stream_session = AgentSession.create(
        store=store,
        session_id="sess_observer_stream",
        agents_md="",
        tools=[_echo_tool()],
    )
    stream_session.permission_coordinator.set_mode(PermissionMode.BYPASS)
    stream_provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[ToolCall(id="call_echo_2", name="echo", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-seq", model="fake-seq-model", content="streamed"),
        ]
    )
    stream_loop = make_loop(session=stream_session, provider=stream_provider, observer=observer)
    asyncio.run(stream_loop.run_user_turn("echo", streaming=True))

    assert any(isinstance(event, ChatStreamEvent) for event in observer.stream_events)


def test_mcp_activation_tracker_turn_boundary(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    caller = _ContractMcpCaller()
    session = AgentSession.create(
        store=store,
        session_id="sess_mcp_turn_boundary",
        agents_md="",
        tools=_contract_mcp_tools(caller),
    )
    session.permission_coordinator.set_mode(PermissionMode.BYPASS)
    provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[ToolCall(id="call_search", name="mcp_tool_search", arguments={"query": "read github issue"})],
            ),
            ChatResponse(provider="fake-seq", model="fake-seq-model", content="first done"),
            ChatResponse(provider="fake-seq", model="fake-seq-model", content="second done"),
        ]
    )
    loop = make_loop(session=session, provider=provider)

    loop._run_user_turn_sync("Read issue 12")
    loop._run_user_turn_sync("Explain this local function")

    next_turn_names = {definition.name for definition in loop._provider_tool_definitions()}
    assert "mcp_tool_search" in next_turn_names
    assert not any(name.startswith("mcp__") for name in next_turn_names)


def test_resume_rebind_observers(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_rebind_observers",
        agents_md="",
        tools=[create_ask_user_tool(), _echo_tool()],
    )
    provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_ask", name="ask_user", arguments={"question": "继续?", "options": ["y", "n"]}),
                    ToolCall(id="call_echo", name="echo", arguments={}),
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-seq", model="fake-seq-model", content="done"),
        ]
    )
    old_stream: list[ChatStreamEvent] = []
    old_tool: list[ToolExecutionEvent] = []
    new_stream: list[ChatStreamEvent] = []
    new_tool: list[ToolExecutionEvent] = []
    loop = make_loop(
        session=session,
        provider=provider,
        stream_event_handler=old_stream.append,
        tool_event_handler=old_tool.append,
    )

    first = asyncio.run(loop.run_user_turn("部署", streaming=True))
    assert first.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert old_stream
    assert any(event.kind == "started" for event in old_tool)

    loop.stream_event_handler = new_stream.append
    loop.tool_event_handler = new_tool.append

    resumed = asyncio.run(loop._resume_with_user_input_streaming(first.pending_input.id, "y"))
    assert resumed.status == AgentTurnStatus.COMPLETED
    assert any(event.kind == "finished" and event.tool_call.name == "echo" for event in new_tool)
    assert not any(event.tool_call.name == "echo" for event in old_tool)
    assert new_stream


def test_resume_outcome_discriminant(tmp_path, make_loop) -> None:
    """PermissionResumeHandler 的三值判别式：continue / wait_for_input / finished。

    三用例都经 loop 的入口 glue 走生产路径，断言落在返回的 AgentTurnResult 上：
    - 整批跑完 → continue：resume 后重新进入工具循环，最终 COMPLETED。
    - 链式 pending → wait_for_input：延迟批次里又有 ask_user，结果 WAITING_FOR_USER_INPUT。
    - request_id 不匹配 → finished：结果 COMPLETED 且 finish_reason == "error"。
    """
    store = JsonlSessionStore(tmp_path)

    # continue：单 ask_user 恢复后无延迟批次，重新进入工具循环得到最终回答。
    continue_session = AgentSession.create(
        store=store,
        session_id="sess_resume_continue",
        agents_md="",
        tools=[create_ask_user_tool()],
    )
    continue_provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[ToolCall(id="call_ask", name="ask_user", arguments={"question": "继续?", "options": ["y", "n"]})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-seq", model="fake-seq-model", content="done"),
        ]
    )
    continue_loop = make_loop(session=continue_session, provider=continue_provider)
    paused = continue_loop._run_user_turn_sync("部署")
    assert paused.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    resumed = continue_loop._resume_with_user_input_sync(paused.pending_input.id, "y")
    assert resumed.status == AgentTurnStatus.COMPLETED
    assert resumed.response is not None
    assert resumed.response.content == "done"

    # wait_for_input：两个 ask_user，回答第一个后延迟批次里的第二个再次暂停。
    chain_session = AgentSession.create(
        store=store,
        session_id="sess_resume_chain",
        agents_md="",
        tools=[create_ask_user_tool()],
    )
    chain_provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_ask_1", name="ask_user", arguments={"question": "继续?", "options": ["y", "n"]}),
                    ToolCall(id="call_ask_2", name="ask_user", arguments={"question": "再来?", "options": ["y", "n"]}),
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    chain_loop = make_loop(session=chain_session, provider=chain_provider)
    paused = chain_loop._run_user_turn_sync("部署")
    assert paused.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    resumed = chain_loop._resume_with_user_input_sync(paused.pending_input.id, "y")
    assert resumed.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert resumed.pending_input is not None
    assert resumed.pending_input.id == "call_ask_2"

    # finished：request_id 与已有 pending 不匹配，返回携带错误响应的结果。
    wrong_session = AgentSession.create(
        store=store,
        session_id="sess_resume_wrong",
        agents_md="",
        tools=[create_ask_user_tool()],
    )
    wrong_provider = _SequenceProvider(
        [
            ChatResponse(
                provider="fake-seq",
                model="fake-seq-model",
                content="",
                tool_calls=[ToolCall(id="call_ask", name="ask_user", arguments={"question": "继续?", "options": ["y", "n"]})],
                finish_reason="tool_calls",
            )
        ]
    )
    wrong_loop = make_loop(session=wrong_session, provider=wrong_provider)
    paused = wrong_loop._run_user_turn_sync("部署")
    assert paused.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    resumed = wrong_loop._resume_with_user_input_sync("wrong_request_id", "y")
    assert resumed.status == AgentTurnStatus.COMPLETED
    assert resumed.finish_reason == "error"


def test_loop_tool_executor_attribute_contract(tmp_path, make_loop) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_executor_attr",
        agents_md="",
    )
    provider = _SequenceProvider([ChatResponse(provider="fake-seq", model="fake-seq-model", content="hi")])
    loop = make_loop(session=session, provider=provider)

    assert isinstance(loop.tool_executor, ToolExecutor)
    assert callable(loop.tool_executor.execute_interactive)


def test_mcp_activation_tracker_contract() -> None:
    tracker = McpActivationTracker(frozenset({"mcp__a"}))

    denied = tracker.validate(ToolCall(id="call_a", name="mcp__a", arguments={}))
    assert denied is not None
    assert denied.data.get("mcp_activation_required") is True
    assert tracker.validate(ToolCall(id="call_ls", name="ls", arguments={})) is None

    activated = ToolResult(
        name="mcp_tool_search",
        ok=True,
        content="ok",
        data={"mcp_tool_search": {"activated_tools": ["mcp__a"]}},
    )
    tracker.observe(ToolCall(id="call_search", name="mcp_tool_search", arguments={"query": "a"}), activated)
    assert tracker.validate(ToolCall(id="call_a_2", name="mcp__a", arguments={})) is None
    assert tracker.active_names == frozenset({"mcp__a"})
    assert tracker.mcp_tool_names == frozenset({"mcp__a"})

    tracker.clear()
    assert tracker.active_names == frozenset()
    assert tracker.validate(ToolCall(id="call_a_3", name="mcp__a", arguments={})) is not None


def test_run_user_turn_contract_signature() -> None:
    params = list(inspect.signature(AgentLoop.run_user_turn).parameters)
    assert params == ["self", "content", "attachments", "streaming"]


def test_session_turn_runner_protocol_contract(make_loop, tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_turn_runner_protocol", agents_md="")
    loop = make_loop(session=session, provider=FakeProvider())

    assert isinstance(loop, SessionTurnRunner)
    assert "content" in inspect.signature(loop.run_user_turn).parameters


def test_sync_facade_contract_signature() -> None:
    assert list(inspect.signature(AgentLoop._run_user_turn_sync).parameters) == [
        "self",
        "content",
        "attachments",
    ]
    assert list(inspect.signature(AgentLoop._run_nudge_turn_sync).parameters) == ["self"]
    assert list(inspect.signature(AgentLoop._resume_with_user_input_sync).parameters) == [
        "self",
        "request_id",
        "answer",
    ]
    for facade in (
        AgentLoop._run_user_turn_sync,
        AgentLoop._run_nudge_turn_sync,
        AgentLoop._resume_with_user_input_sync,
    ):
        # loop.py 用 `from __future__ import annotations`，注解是字符串，
        # 经 get_type_hints 求值后应是同一个 AgentTurnResult 类。
        assert typing.get_type_hints(facade).get("return") is AgentTurnResult


def test_subagent_engine_no_module_loop_import() -> None:
    """Importing ``subagent_engine`` must not pull in ``agent.loop``.

    The child loop is produced by the injected ``child_runner_factory``, so the
    engine no longer lazily imports ``AgentLoop``. Asserted in a fresh
    interpreter because this test module already imports both.
    """

    code = (
        "import sys\n"
        "import lanscoder.agent.subagent_engine\n"
        "if 'lanscoder.agent.loop' in sys.modules:\n"
        "    sys.stderr.write('agent.loop leaked: %r\\n' % [m for m in sys.modules if m == 'lanscoder.agent.loop'])\n"
        "    raise SystemExit(1)\n"
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_subagent_runner_protocol_contract(tmp_path) -> None:
    provider = FakeProvider()
    store = JsonlSessionStore(tmp_path)
    host = AgentSession.create(store=store, session_id="sess_engine_host", agents_md="")
    engine = SubagentEngine(
        store=store,
        provider=provider,
        tools=[],
        permission_coordinator=host.permission_coordinator,
        child_runner_factory=lambda **kwargs: None,
    )

    assert isinstance(engine, SubagentRunner)
    assert engine.foreground_progress is None


def test_make_loop_uses_production_constructor(make_loop, tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_make_loop_prod", agents_md="")
    loop = make_loop(session=session, provider=FakeProvider())

    assert isinstance(loop, AgentLoop)
    assert loop.request_builder is not None
    assert loop.guardrails is not None
    assert loop._observer is not None
    assert loop.tool_executor is not None
    assert loop._mcp_activation is not None
    # AgentChatRunner 的 per-turn loop 工厂与组装根 create_agent_loop 同源。
    assert inspect.getmodule(AgentChatRunner._create_loop) is inspect.getmodule(create_agent_loop)


def test_permission_coordinator_contract_signature() -> None:
    """钉 PermissionCoordinator 六个核心方法的参数名（缝 4 接口契约）。"""

    assert list(inspect.signature(PermissionCoordinator.set_mode).parameters) == ["self", "mode"]
    assert list(inspect.signature(PermissionCoordinator.preflight).parameters) == ["self", "tool_call"]
    assert list(inspect.signature(PermissionCoordinator.prepare).parameters) == ["self", "tool_call", "deferred_tool_calls"]
    assert list(inspect.signature(PermissionCoordinator.store_pending_request).parameters) == ["self", "tool_call", "request", "deferred_tool_calls", "review_only"]
    assert list(inspect.signature(PermissionCoordinator.requires_review).parameters) == ["self", "tool_call"]
    assert list(inspect.signature(PermissionCoordinator.bypass_mutation).parameters) == ["self", "tool_call", "preflight"]


class _RecordingPermissionCoordinator(PermissionCoordinator):
    """记录一次 prepare 编排内部的方法调用顺序。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def preflight(self, tool_call):
        self.calls.append("preflight")
        return super().preflight(tool_call)

    def requires_review(self, tool_call):
        self.calls.append("requires_review")
        return super().requires_review(tool_call)

    def store_pending_request(self, **kwargs):
        self.calls.append("store_pending_request")
        return super().store_pending_request(**kwargs)

    def bypass_mutation(self, tool_call, *, preflight):
        self.calls.append("bypass_mutation")
        return super().bypass_mutation(tool_call, preflight=preflight)


def test_permission_coordinator_call_order(tmp_path) -> None:
    """钉缝 4 行为不变式：preflight →（requires_review 时）store_pending_request → bypass_mutation。"""

    store = JsonlSessionStore(tmp_path / ".lanscoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_coord_order",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    base = session.permission_coordinator
    coordinator = _RecordingPermissionCoordinator(
        session=session,
        permission_manager=base.permission_manager,
        sandbox_access=base.sandbox_access,
    )
    session.permission_coordinator = coordinator
    tool_call = ToolCall(id="call_write", name="write", arguments={"path": "README.md", "content": "hello"})

    # STANDARD：write 需要确认，preflight 后直接 store_pending_request，不触发 bypass。
    prepared = coordinator.prepare(tool_call, [])
    assert coordinator.calls == ["preflight", "store_pending_request"]
    assert prepared.pending_input is not None
    assert prepared.result is None
    assert "bypass_mutation" not in coordinator.calls

    # BYPASS：write 放行但需要 bypass 写前预览，requires_review 之后走 bypass_mutation。
    coordinator.set_mode(PermissionMode.BYPASS)
    coordinator.calls.clear()
    prepared = coordinator.prepare(tool_call, [])
    assert coordinator.calls == ["preflight", "requires_review", "bypass_mutation"]
    assert prepared.pending_input is None
    assert prepared.result is None
    assert "store_pending_request" not in coordinator.calls


def test_coordinator_pending_store_contract(tmp_path) -> None:
    """CoordinatorPendingStore 经 coordinator 的 pending_get/pending_clear 读写 session pending。"""

    store = JsonlSessionStore(tmp_path / ".lanscoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_pending_swap",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    coordinator = session.permission_coordinator
    tool_call = ToolCall(id="call_write", name="write", arguments={"path": "README.md", "content": "hello"})
    prepared = coordinator.prepare(tool_call, [])
    assert prepared.pending_input is not None

    pending_store = CoordinatorPendingStore(coordinator)
    request_id = session.pending_permission_execution.request_id

    assert pending_store.get(request_id) is session.pending_permission_execution
    assert pending_store.get("wrong_request_id") is None

    pending_store.clear()
    assert pending_store.get(request_id) is None
    assert session.pending_permission_execution is None
