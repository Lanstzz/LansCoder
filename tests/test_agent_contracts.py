"""Loop 协作对象契约测试：签名固定 + 纯变换行为（零 loop 状态）。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field

import pytest

from lanscoder.agent._builders import register_loop_tools
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.guardrails import TurnGuardrails
from lanscoder.agent.loop import AgentLoop
from lanscoder.agent.loop_limits import _AgentLoopLimitReached, AgentLoopLimits
from lanscoder.agent.mcp_activation import McpActivationTracker
from lanscoder.agent.request_builder import PreparedMainRequest, RequestBuilder
from lanscoder.agent.session import AgentSession
from lanscoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from lanscoder.agent.user_input import AgentTurnStatus
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
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.tools.types import Tool, ToolResult, make_text_result


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


def test_registry_complete_before_loop(tmp_path) -> None:
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

        # AgentLoop 构造之前，registry 已是单一工具源：caller + 后台控制 + delegate 全齐。
        names = session.tool_registry.names()
        for expected in ("caller_tool", "background_status", "background_cancel", "delegate"):
            assert expected in names
        # runtime 通过 _current_tools() 喂入的每个工具，register_loop_tools 都已放入 registry。
        for tool in [caller_tool]:
            assert tool.name in names

        # 工具注册完全上移到组装根：构造 AgentLoop 期间 register 零新增。
        original_register = session.tool_registry.register
        register_calls: list[str] = []

        def _spy_register(tool):
            register_calls.append(tool.name)
            return original_register(tool)

        session.tool_registry.register = _spy_register
        AgentLoop(session=session, provider=provider)
        assert register_calls == []
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


def test_observer_consumer_pipeline(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_observer_pipeline",
        agents_md="",
        tools=[_echo_tool()],
    )
    session.set_permission_mode(PermissionMode.BYPASS)
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
    loop = AgentLoop(session=session, provider=provider, observer=observer)

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
    stream_session.set_permission_mode(PermissionMode.BYPASS)
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
    stream_loop = AgentLoop(session=stream_session, provider=stream_provider, observer=observer)
    asyncio.run(stream_loop.run_user_turn("echo", streaming=True))

    assert any(isinstance(event, ChatStreamEvent) for event in observer.stream_events)


def test_mcp_activation_tracker_turn_boundary(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    caller = _ContractMcpCaller()
    session = AgentSession.create(
        store=store,
        session_id="sess_mcp_turn_boundary",
        agents_md="",
        tools=_contract_mcp_tools(caller),
    )
    session.set_permission_mode(PermissionMode.BYPASS)
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
    loop = AgentLoop(session=session, provider=provider)

    loop._run_user_turn_sync("Read issue 12")
    loop._run_user_turn_sync("Explain this local function")

    next_turn_names = {definition.name for definition in loop._provider_tool_definitions()}
    assert "mcp_tool_search" in next_turn_names
    assert not any(name.startswith("mcp__") for name in next_turn_names)


def test_resume_rebind_observers(tmp_path) -> None:
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
    loop = AgentLoop(
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


def test_loop_tool_executor_attribute_contract(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_executor_attr",
        agents_md="",
    )
    provider = _SequenceProvider([ChatResponse(provider="fake-seq", model="fake-seq-model", content="hi")])
    loop = AgentLoop(session=session, provider=provider)

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
