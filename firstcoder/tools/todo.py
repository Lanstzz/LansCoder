"""无状态的 Session Todo 完整列表工具。"""

from __future__ import annotations

from typing import Any

from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema


VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")
VALID_PRIORITIES = ("high", "medium", "low")
LEGACY_STATUS_ALIASES = {"done": "completed"}


def create_todo_tool() -> Tool:
    """创建只负责验证和规范化完整列表的 Todo 工具。"""

    def todo(todos: list[dict[str, Any]]) -> ToolResult:
        normalized, error = _normalize_todos(todos)
        if error is not None:
            return make_error_result("todo", error)
        return _format_result(normalized)

    return Tool(
        definition=ToolDefinition(
            name="todo",
            description=(
                "Track multi-step work by submitting the complete current list on every call. "
                "For substantial coding tasks, use a concise 3-7 item plan with exactly one "
                "in_progress item while work is active. Preserve item content and order during "
                "routine status updates; only add, remove, rewrite, or reorder items when the plan "
                "itself changes. Before a final answer, complete or cancel every remaining item, "
                "or clearly explain the blocker. An empty list clears the session Todo state."
            ),
            parameters=object_schema(
                {
                    "todos": {
                        "type": "array",
                        "description": "The complete current Todo list, in display and execution order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Short, concrete, verifiable task."},
                                "status": {"type": "string", "enum": list(VALID_STATUSES)},
                                "priority": {"type": "string", "enum": list(VALID_PRIORITIES)},
                            },
                            "required": ["content", "status", "priority"],
                            "additionalProperties": False,
                        },
                    }
                },
                required=["todos"],
            ),
        ),
        executor=todo,
    )


def _normalize_todos(todos: object) -> tuple[list[dict[str, str]], str | None]:
    if not isinstance(todos, list):
        return [], "todos 必须是数组"

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            return [], f"todos[{index}] 必须是对象"
        content = str(item.get("content") or "").strip()
        if not content:
            return [], f"todos[{index}] 缺少 content"
        status = str(item.get("status") or "pending")
        status = LEGACY_STATUS_ALIASES.get(status, status)
        if status not in VALID_STATUSES:
            return [], f"todos[{index}] 未知状态：{status}"
        priority = str(item.get("priority") or "medium")
        if priority not in VALID_PRIORITIES:
            return [], f"todos[{index}] 未知优先级：{priority}"
        normalized.append({"content": content, "status": status, "priority": priority})
    return normalized, None


def _format_result(todos: list[dict[str, str]]) -> ToolResult:
    markers = {
        "pending": "[ ]",
        "in_progress": "[~]",
        "completed": "[✓]",
        "cancelled": "[-]",
    }
    lines = ["已更新任务清单"]
    lines.extend(f"{markers[item['status']]} {item['content']}" for item in todos)
    return make_text_result("todo", "\n".join(lines), todos=todos, count=len(todos))
