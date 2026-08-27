"""Step 2 L2: Agent 有状态 wrapper(subscribe / prompt / steer / follow_up / abort)。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lanscoder.core.agent import Agent
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
async def test_agent_prompt_dispatches_events_to_subscribers() -> None:
    agent = Agent(
        LoopContext(),
        LoopConfig(provider=_provider("hi"), session_id="s1"),
    )
    seen: list[str] = []
    agent.subscribe(lambda event: seen.append(event.type))
    await agent.prompt("hello")
    assert seen[0] == "agent_start"
    assert seen[-1] == "agent_end"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_unsubscribe_stops_events() -> None:
    agent = Agent(
        LoopContext(),
        LoopConfig(provider=_provider("hi"), session_id="s1"),
    )
    seen: list[str] = []
    unsubscribe = agent.subscribe(lambda event: seen.append(event.type))
    unsubscribe()
    await agent.prompt("hello")
    assert seen == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_prompt_accumulates_messages_in_context() -> None:
    agent = Agent(
        LoopContext(),
        LoopConfig(provider=_provider("hi"), session_id="s1"),
    )
    await agent.prompt("hello")
    roles = [message.role for message in agent._context.messages]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_steer_runs_after_prompt_turn() -> None:
    agent = Agent(
        LoopContext(),
        LoopConfig(provider=_provider("first", "steered"), session_id="s1"),
    )
    agent.steer(LoopMessage.user("steer me"))
    await agent.prompt("hello")
    contents = [
        m.content for r in agent._config.provider.requests for m in r.messages  # type: ignore[attr-defined]
    ]
    assert any("steer me" in content for content in contents)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_follow_up_runs_after_turns() -> None:
    agent = Agent(
        LoopContext(),
        LoopConfig(provider=_provider("first", "follow"), session_id="s1"),
    )
    agent.follow_up(LoopMessage.user("follow me"))
    await agent.prompt("hello")
    contents = [
        m.content for r in agent._config.provider.requests for m in r.messages  # type: ignore[attr-defined]
    ]
    assert any("follow me" in content for content in contents)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_abort_cancels_active_run() -> None:
    agent = Agent(
        LoopContext(),
        LoopConfig(provider=_provider("hi"), session_id="s1"),
    )
    agent.abort()
    assert agent._cancellation.is_cancelled
