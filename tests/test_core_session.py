"""Step 2 L3: create_agent_session headless 装配源。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lanscoder.core import AgentSessionHandle, LoopConfig, LoopContext, create_agent_session
from lanscoder.core.agent import Agent
from lanscoder.core.runtime import AgentChatRunner
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


@pytest.mark.anyio
async def test_create_agent_session_assembles_handle(tmp_path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="m", content="hi")]
    )
    handle = create_agent_session(provider=provider, project_root=tmp_path)
    assert isinstance(handle, AgentSessionHandle)
    assert handle.session.session_id
    assert isinstance(handle.runner, AgentChatRunner)
    assert not hasattr(handle, "agent")  # D2: handle 只留 session + runner


@pytest.mark.anyio
async def test_create_agent_session_builds_builtin_tools(tmp_path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="m", content="hi")]
    )
    handle = create_agent_session(provider=provider, project_root=tmp_path)
    names = set(handle.session.tool_registry.names())
    assert {"view", "write", "edit", "shell"} <= names


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_l2_agent_prompt_runs_headless_without_handle(tmp_path) -> None:
    """SC-5: handle 去掉 agent 后,L2 Agent 独立可用(session-free,不经 L3 handle)。"""

    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="m", content="hello from L2")]
    )
    agent = Agent(
        context=LoopContext(system_prompt="", messages=[], tools=[]),
        config=LoopConfig(provider=provider, session_id="l2-headless"),
    )
    seen: list[str] = []
    agent.subscribe(lambda event: seen.append(event.type))
    await agent.prompt("hello")
    assert seen[0] == "agent_start"
    assert seen[-1] == "agent_end"


@pytest.mark.anyio
async def test_resume_reuses_session_when_id_given(tmp_path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="m", content="hi")]
    )
    first = create_agent_session(provider=provider, project_root=tmp_path, session_id="keep")
    second = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        session_id="keep",
        resume=True,
    )
    assert first.session.session_id == "keep"
    assert second.session.session_id == "keep"
    assert second.session is not first.session


@pytest.mark.anyio
async def test_create_agent_session_wires_compaction_strategy(tmp_path) -> None:
    """compaction_strategy 透传到 ContextWindowManager(默认保持全量)。"""

    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="m", content="hi")]
    )
    handle = create_agent_session(
        provider=provider,
        project_root=tmp_path,
        compaction_strategy="no_compact",
    )
    from lanscoder.context.manager import CompactionStrategy

    assert handle.runner.context_manager.strategy == CompactionStrategy.NO_COMPACT

    default = create_agent_session(provider=provider, project_root=tmp_path)
    assert default.runner.context_manager.strategy == CompactionStrategy.L1_L2_L3


@pytest.mark.anyio
async def test_create_agent_session_rejects_unknown_compaction_strategy(tmp_path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="m", content="hi")]
    )
    with pytest.raises(ValueError):
        create_agent_session(
            provider=provider,
            project_root=tmp_path,
            compaction_strategy="bogus",
        )
