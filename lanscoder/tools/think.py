from __future__ import annotations

from lanscoder.tools.types import Tool, ToolResult, make_text_result
from lanscoder.utils.introspection import tool_from_function


def create_think_tool() -> Tool:

    def think(thought: str) -> ToolResult:
        """记录内部思考；不访问外部资源，不修改状态。"""

        return make_text_result("think", thought, thought=thought)

    return tool_from_function(think)
