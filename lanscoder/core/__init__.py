"""LansCoder 三层解耦核心 API。

- L1 `agent_loop`: 无状态裸循环,`AsyncIterator[AgentEvent]`,不碰 session/持久化/TUI。
- L2 `Agent`: 有状态 wrapper,`subscribe / prompt / steer / follow_up / abort`。
- L3 `create_agent_session`: headless 唯一装配源,返回 `AgentSessionHandle`。

约束: core 永不 import app。
"""

from lanscoder.core.agent import Agent
from lanscoder.core.agent_loop import agent_loop
from lanscoder.core.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from lanscoder.core.messages import LoopConfig, LoopContext, LoopMessage, convert_to_llm
from lanscoder.core.session import AgentSessionHandle, create_agent_session
from lanscoder.core.transport import LlmTransport

__all__ = [
    "Agent",
    "AgentEndEvent",
    "AgentEvent",
    "AgentSessionHandle",
    "AgentStartEvent",
    "LoopConfig",
    "LoopContext",
    "LlmTransport",
    "LoopMessage",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TurnEndEvent",
    "TurnStartEvent",
    "agent_loop",
    "convert_to_llm",
    "create_agent_session",
]
