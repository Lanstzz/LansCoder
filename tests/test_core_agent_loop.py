"""Step 2 L1: agent_loop 事件流(无状态、session-free、不落持久化)。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lanscoder.core.agent_loop import agent_loop
from lanscoder.core.events import AgentEndEvent
from lanscoder.core.messages import LoopConfig, LoopContext, LoopMessage
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse


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
