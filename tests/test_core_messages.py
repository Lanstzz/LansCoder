"""Step 2 契约: LoopMessage / LoopContext / LoopConfig / convert_to_llm(D2)。"""

from __future__ import annotations

from lanscoder.core.messages import LoopContext, LoopMessage, convert_to_llm
from lanscoder.providers.types import ChatMessage, ToolCall


def test_loop_message_factories() -> None:
    assert LoopMessage.user("hi").role == "user"
    assert LoopMessage.assistant("yo").role == "assistant"
    tool = LoopMessage.tool("ok", tool_call_id="tc-1")
    assert tool.role == "tool"
    assert tool.tool_call_id == "tc-1"


def test_convert_to_llm_maps_fields() -> None:
    msg = LoopMessage.tool(
        "result",
        tool_call_id="tc-1",
        tool_calls=[ToolCall(id="tc-1", name="read", arguments={})],
        name="read",
    )
    llm = convert_to_llm(msg)
    assert isinstance(llm, ChatMessage)
    assert llm.role == "tool"
    assert llm.content == "result"
    assert llm.tool_call_id == "tc-1"
    assert llm.name == "read"
    assert len(llm.tool_calls) == 1
    assert llm.tool_calls[0].name == "read"


def test_convert_to_llm_preserves_identity_of_tool_calls() -> None:
    tool_call = ToolCall(id="a", name="grep", arguments={"q": "x"})
    msg = LoopMessage.assistant("", tool_calls=[tool_call])
    llm = convert_to_llm(msg)
    assert llm.tool_calls[0] is tool_call


def test_loop_context_and_config_defaults() -> None:
    context = LoopContext()
    assert context.system_prompt == ""
    assert context.messages == []
    assert context.tools == []
