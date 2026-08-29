"""最小 ``LlmTransport`` 接入示例(D3 / SC-4)。

不继承 ``ChatProvider``,只实现协议要求的 2 方法 + 3 属性,即可驱动 L1
``agent_loop`` 与 L2 ``Agent``。运行::

    python examples/sdk/minimal_llm_transport.py

期望输出: 事件序列以 ``agent_start`` 开头、``agent_end`` 收尾。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

# 允许从仓库根目录直接运行(未安装时);已安装时这行无副作用。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lanscoder.core import Agent, LoopConfig, LoopContext
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderCapabilities,
)


@dataclass
class StubTransport:
    """duck-typed transport: ``name / model / capabilities`` + ``complete / astream``。"""

    name: str = "stub"
    model: str = "stub-model"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    replies: list[str] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(provider=self.name, model=self.model, content=self.replies.pop(0))

    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        async def _stream() -> AsyncIterator[ChatStreamEvent]:
            for chunk in self.replies.pop(0).split(" "):
                yield ChatStreamEvent(kind="text_delta", text=chunk + " ")

        return _stream()


async def main() -> None:
    transport = StubTransport(replies=["你好,我是 duck-typed LlmTransport!"])

    # L2 Agent 内部驱动 L1 agent_loop;transport 无需继承任何 ABC。
    agent = Agent(
        context=LoopContext(system_prompt="你是最小示例 agent。"),
        config=LoopConfig(provider=transport, session_id="minimal-transport"),
    )
    seen: list[str] = []
    agent.subscribe(lambda event: seen.append(event.type))
    await agent.prompt("打个招呼")
    print("事件序列:", seen)
    assert seen[0] == "agent_start" and seen[-1] == "agent_end"


if __name__ == "__main__":
    asyncio.run(main())
