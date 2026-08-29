"""Step 2 L1: agent_loop 事件流(无状态、session-free、不落持久化)。"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field

import pytest

from lanscoder.agent.session import AgentSession
from lanscoder.context.store import InMemorySessionStore
from lanscoder.core.agent_loop import agent_loop
from lanscoder.core.events import AgentEndEvent
from lanscoder.core.messages import LoopConfig, LoopContext, LoopMessage
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderCapabilities,
    ToolCall,
)
from lanscoder.tools.types import Tool, ToolDefinition, ToolResult


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _provider(*contents: str) -> FakeProvider:
    return FakeProvider(
        responses=[
            ChatResponse(provider="fake", model="fake-model", content=content)
            for content in contents
        ]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_emits_lifecycle_events_in_order() -> None:
    provider = _provider("hello back")
    events = [
        event
        async for event in agent_loop(
            [LoopMessage.user("hello")],
            LoopContext(system_prompt="sys"),
            LoopConfig(provider=provider, session_id="s1"),
        )
    ]
    types = [event.type for event in events]
    assert types[0] == "agent_start"
    assert "turn_start" in types
    assert "message_start" in types
    assert "message_end" in types
    assert "turn_end" in types
    assert types[-1] == "agent_end"
    # agent_start 在前、agent_end 收尾
    assert types.index("agent_start") < types.index("turn_start")
    assert types.index("turn_end") < types.index("agent_end")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_end_carries_session_messages() -> None:
    provider = _provider("hi back")
    events = [
        event
        async for event in agent_loop(
            [LoopMessage.user("hello")],
            LoopContext(),
            LoopConfig(provider=provider, session_id="s1"),
        )
    ]
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    roles = [message.role for message in end.messages]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_multiple_prompts_produce_multiple_turns() -> None:
    provider = _provider("one", "two")
    events = [
        event
        async for event in agent_loop(
            [LoopMessage.user("q1"), LoopMessage.user("q2")],
            LoopContext(),
            LoopConfig(provider=provider, session_id="s1"),
        )
    ]
    types = [event.type for event in events]
    assert types.count("turn_start") == 2
    assert types.count("turn_end") == 2
    assert types[-1] == "agent_end"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_empty_prompts_yields_nothing() -> None:
    events = [
        event
        async for event in agent_loop(
            [],
            LoopContext(),
            LoopConfig(provider=_provider("unused"), session_id="s1"),
        )
    ]
    assert events == []


def _fail_temp_dir(*args, **kwargs):  # pragma: no cover - 仅用于证明不落盘
    raise AssertionError("agent_loop must not create temp directories")


def _echo_tool() -> Tool:
    def execute(text: str) -> ToolResult:
        return ToolResult(name="echo", ok=True, content=f"echo:{text}")

    return Tool(
        definition=ToolDefinition(
            name="echo",
            description="回显文本",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        executor=execute,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_uses_in_memory_store_and_no_temp_dirs(monkeypatch) -> None:
    """SC-1: agent_loop 不写盘——任何 TemporaryDirectory 创建都会让本测试失败。"""

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _fail_temp_dir)
    provider = _provider("hello back")
    events = [
        event
        async for event in agent_loop(
            [LoopMessage.user("hello")],
            LoopContext(system_prompt="sys"),
            LoopConfig(provider=provider, session_id="s1"),
        )
    ]
    assert events[-1].type == "agent_end"
    assert provider.requests  # 正常驱动了 provider


def test_inmemory_session_store_rebuilds_and_never_writes() -> None:
    """SC-1: InMemorySessionStore 本身不建目录、不写盘,且能重建/截断/删除。"""

    store = InMemorySessionStore()
    sentinel = store.root
    assert not sentinel.exists()
    session = AgentSession.create(store=store, session_id="mem-1", agents_md="")
    session.append_user_message("hello")

    view = store.rebuild_session_view("mem-1")
    assert [message.role for message in view.messages] == ["user"]
    assert store.original_user_message_texts("mem-1")[view.messages[0].id] == "hello"
    assert not sentinel.exists()

    user_id = view.messages[0].id
    assert store.truncate_before_message("mem-1", user_id) == 1  # 只留 session_created
    assert store.delete_session("mem-1") is True
    assert store.delete_session("mem-1") is False
    assert not sentinel.exists()
    assert list(sentinel.parent.glob(f"{sentinel.name}*")) == []


@dataclass
class StreamingProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)
    complete_calls: int = 0

    @property
    def name(self) -> str:
        return "fake-stream"

    @property
    def model(self) -> str:
        return "fake-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.complete_calls += 1
        self.requests.append(request)
        return self.responses.pop(0)

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        response = self.responses.pop(0)
        if response.content:
            yield ChatStreamEvent(kind="text_delta", text=response.content)
        yield ChatStreamEvent(kind="message_completed", response=response)


def _streaming_provider(*, supports_streaming: bool, contents: tuple[str, ...] = ("hi back",)) -> StreamingProvider:
    return StreamingProvider(
        responses=[
            ChatResponse(provider="fake-stream", model="m", content=content)
            for content in contents
        ],
        capabilities=ProviderCapabilities(supports_streaming=supports_streaming),
    )


async def _collect(provider: ChatProvider, **config_kwargs):
    return [
        event
        async for event in agent_loop(
            [LoopMessage.user("hello")],
            LoopContext(system_prompt="sys"),
            LoopConfig(provider=provider, session_id="s1", **config_kwargs),
        )
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_streaming_auto_detects_supports_streaming() -> None:
    """SC-2: use_streaming=None 且 provider 支持流式 → 自动走流式,有 message_update。"""

    provider = _streaming_provider(supports_streaming=True)
    events = await _collect(provider)
    types = [event.type for event in events]
    assert "message_update" in types
    assert provider.complete_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_streaming_forced_true() -> None:
    """SC-2: use_streaming=True 强制流式(即使 capabilities 不支持),有 message_update。"""

    provider = _streaming_provider(supports_streaming=False)
    events = await _collect(provider, use_streaming=True)
    types = [event.type for event in events]
    assert "message_update" in types
    assert provider.complete_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_streaming_forced_false_only_message_end() -> None:
    """SC-2: use_streaming=False 强制非流式——只有 message_end,没有 message_update。"""

    provider = _streaming_provider(supports_streaming=True)
    events = await _collect(provider, use_streaming=False)
    types = [event.type for event in events]
    assert "message_update" not in types
    assert types.count("message_end") == 1
    assert provider.complete_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_streaming_auto_detects_unsupported() -> None:
    """SC-2: use_streaming=None 且 provider 不支持流式 → 自动走非流式,无 message_update。"""

    provider = _streaming_provider(supports_streaming=False)
    events = await _collect(provider)
    types = [event.type for event in events]
    assert "message_update" not in types
    assert provider.complete_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_tool_multi_roundtrip_without_permission_manager() -> None:
    """SC-3: L1 保留工具多轮往返,permission_manager=None 时不承担权限/守卫。"""

    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="m",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="m", content="完成"),
        ]
    )
    events = [
        event
        async for event in agent_loop(
            [LoopMessage.user("调用工具")],
            LoopContext(tools=[_echo_tool()]),
            LoopConfig(provider=provider, session_id="s1"),
        )
    ]
    types = [event.type for event in events]
    assert "tool_execution_start" in types
    assert "tool_execution_end" in types
    assert len(provider.requests) == 2
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert [message.role for message in end.messages] == ["user", "assistant", "tool", "assistant"]
