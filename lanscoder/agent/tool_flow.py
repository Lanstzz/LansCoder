from __future__ import annotations

from lanscoder.context.identity import new_part_id
from lanscoder.context.models import MessagePart
from lanscoder.context.tool_sequence import InvalidToolCallSequenceError, validate_tool_call_sequence
from lanscoder.context.writer import tool_call_to_part
from lanscoder.providers.types import ChatResponse, ToolCall
from lanscoder.tools.types import ToolResult

__all__ = [
    "assistant_response_to_parts",
    "tool_result_to_part",
    "InvalidToolCallSequenceError",
    "validate_tool_call_sequence",
]


def assistant_response_to_parts(*, message_id: str, response: ChatResponse) -> list[MessagePart]:

    parts: list[MessagePart] = []
    if response.content:
        parts.append(MessagePart(id=new_part_id(), message_id=message_id, kind="text", content=response.content))
    for tool_call in response.tool_calls:
        parts.append(tool_call_to_part(message_id=message_id, tool_call=tool_call))
    return parts


def tool_result_to_part(*, message_id: str, tool_call: ToolCall, result: ToolResult) -> MessagePart:

    return MessagePart(
        id=new_part_id(),
        message_id=message_id,
        kind="tool_result",
        content=result.content,
        metadata={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
        },
    )
