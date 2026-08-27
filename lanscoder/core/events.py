"""L1/L2 事件模型(D3):照搬 pi 的 10 种 AgentEvent。

词汇表(与 pi `packages/agent` 对齐):
agent_start / agent_end、turn_start / turn_end、
message_start / message_update / message_end、
tool_execution_start / tool_execution_update / tool_execution_end。
L3 的会话事件词汇与 L1/L2 不同(pi 原生设计: AgentEvent vs AgentSessionEvent)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Union

if TYPE_CHECKING:
    from lanscoder.providers.types import ChatStreamEvent

    from lanscoder.core.messages import LoopMessage


@dataclass(frozen=True, slots=True)
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass(frozen=True, slots=True)
class AgentEndEvent:
    type: Literal["agent_end"] = "agent_end"
    messages: tuple[LoopMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass(frozen=True, slots=True)
class TurnEndEvent:
    type: Literal["turn_end"] = "turn_end"
    message: LoopMessage | None = None


@dataclass(frozen=True, slots=True)
class MessageStartEvent:
    type: Literal["message_start"] = "message_start"
    message: LoopMessage | None = None


@dataclass(frozen=True, slots=True)
class MessageUpdateEvent:
    type: Literal["message_update"] = "message_update"
    message: LoopMessage | None = None
    assistant_message_event: ChatStreamEvent | None = None


@dataclass(frozen=True, slots=True)
class MessageEndEvent:
    type: Literal["message_end"] = "message_end"
    message: LoopMessage | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionStartEvent:
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str = ""
    tool_name: str = ""
    args: Any = None


@dataclass(frozen=True, slots=True)
class ToolExecutionUpdateEvent:
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str = ""
    tool_name: str = ""
    args: Any = None
    partial_result: Any = None


@dataclass(frozen=True, slots=True)
class ToolExecutionEndEvent:
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str = ""
    tool_name: str = ""
    result: Any = None
    is_error: bool = False


AgentEvent = Union[
    AgentStartEvent,
    AgentEndEvent,
    TurnStartEvent,
    TurnEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
]
