"""L1 消息模型(D2 路线 B)。

core 自建轻量消息类型,命名避开 `context/models.py` 已占用的 `AgentMessage`;
`convert_to_llm` 单向桥接到 provider 的 `ChatMessage`。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.core.transport import LlmTransport
from lanscoder.providers.types import ChatMessage, MainRequestOptions, ToolCall
from lanscoder.tools.types import Tool

# 与 provider 的 MessageRole 对齐,保证 convert_to_llm 类型正确。
LoopRole = Literal["user", "assistant", "tool", "notification", "system_meta"]


@dataclass(frozen=True, slots=True)
class LoopMessage:
    """core 自建轻量消息;L1/L2 的事件与上下文都基于它。"""

    role: LoopRole | str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def user(cls, content: str, **kwargs: Any) -> "LoopMessage":
        return cls(role="user", content=content, **kwargs)

    @classmethod
    def assistant(cls, content: str, **kwargs: Any) -> "LoopMessage":
        return cls(role="assistant", content=content, **kwargs)

    @classmethod
    def tool(cls, content: str, tool_call_id: str, **kwargs: Any) -> "LoopMessage":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, **kwargs)


def convert_to_llm(message: LoopMessage) -> ChatMessage:
    """单向桥接: LoopMessage -> provider ChatMessage(供 RequestBuilder 消费)。"""

    return ChatMessage(
        role=message.role,
        content=message.content,
        name=message.name,
        tool_call_id=message.tool_call_id,
        tool_calls=list(message.tool_calls),
    )


@dataclass(slots=True)
class LoopContext:
    """L1 的最小上下文面(O1=方案 A):显式注入,不暴露 AgentSession。"""

    system_prompt: str = ""
    messages: list[LoopMessage] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)


@dataclass(slots=True)
class LoopConfig:
    """L1 运行配置:provider 显式注入(替代 pi 的 stream_fn 传输抽象,D6)。"""

    provider: LlmTransport  # D3: 传输窄协议化,复用 providers 类型
    session_id: str = ""
    use_streaming: bool | None = None  # D4: None=按 capabilities.supports_streaming 自动;True/False=强制
    request_options: MainRequestOptions | None = None
    context_window: int | None = None
    limits: AgentLoopLimits | None = None
    background_manager: BackgroundJobManager | None = None
    guidance_provider: Callable[[], list[str]] | None = None
