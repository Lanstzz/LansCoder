from __future__ import annotations

from typing import Any

from lanscoder.providers.types import ToolDefinition


def to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def to_anthropic_tool(tool: ToolDefinition) -> dict[str, Any]:

    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }
