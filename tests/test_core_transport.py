"""Step 2 (P3) SC-4: LlmTransport duck-typed transport 驱动 L1;ChatProvider 结构满足。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from lanscoder.core.agent_loop import agent_loop
from lanscoder.core.messages import LoopConfig, LoopContext, LoopMessage
from lanscoder.core.transport import LlmTransport
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderCapabilities,
)


@dataclass
class DuckTransport:
    """非 ChatProvider 的裸 transport:2 方法 + 3 属性(SC-4)。"""

    name: str = "duck"
    model: str = "duck-model"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)
    responses: list[ChatResponse] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        # 非流式路径不会调用;仅满足 Protocol 形状。
        async def _empty() -> AsyncIterator[ChatStreamEvent]:
            if False:
                yield ChatStreamEvent(kind="message_completed")  # pragma: no cover

        return _empty()


class CapableProvider(ChatProvider):
    """带 capabilities 的 ChatProvider 子类,结构上应满足 LlmTransport。"""

    capabilities: ProviderCapabilities = ProviderCapabilities(supports_streaming=True)

    @property
    def name(self) -> str:
        return "capable"

    @property
    def model(self) -> str:
        return "capable-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(provider=self.name, model=self.model, content="ok")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_duck_typed_transport_drives_agent_loop() -> None:
    """SC-4: 非 ChatProvider 子类的 duck-typed transport 可驱动 L1。"""

    transport = DuckTransport(
        responses=[ChatResponse(provider="duck", model="m", content="hi back")]
    )
    events = [
        event
        async for event in agent_loop(
            [LoopMessage.user("hello")],
            LoopContext(system_prompt="sys"),
            LoopConfig(provider=transport, session_id="s1"),
        )
    ]
    assert events[-1].type == "agent_end"
    assert transport.requests  # complete 被正常调用


def test_chat_provider_structurally_satisfies_llm_transport() -> None:
    """SC-4: ChatProvider(含 capabilities 与默认 astream)结构性满足 LlmTransport。"""

    assert isinstance(CapableProvider(), LlmTransport)
    assert isinstance(DuckTransport(), LlmTransport)

