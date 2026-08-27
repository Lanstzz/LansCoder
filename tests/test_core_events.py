"""Step 2 契约: 10 种 AgentEvent(D3)。"""

from __future__ import annotations

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

EXPECTED_TYPES = {
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
}


def test_ten_event_kinds_have_type_discriminator() -> None:
    events = [
        AgentStartEvent(),
        AgentEndEvent(),
        TurnStartEvent(),
        TurnEndEvent(),
        MessageStartEvent(),
        MessageUpdateEvent(),
        MessageEndEvent(),
        ToolExecutionStartEvent(),
        ToolExecutionUpdateEvent(),
        ToolExecutionEndEvent(),
    ]
    assert {event.type for event in events} == EXPECTED_TYPES


def test_agent_event_union_covers_all_kinds() -> None:
    from typing import get_args

    members = {m().type for m in get_args(AgentEvent)}
    assert members == EXPECTED_TYPES
