from __future__ import annotations

import re

from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from lanscoder.utils.introspection import tool_from_function

_OPTION_PREFIX = re.compile(r"^(?:[-*•]|\d+[.)、]|[A-Za-z][.)、])\s*")


def _strip_option_prefix(option: str) -> str:
    return _OPTION_PREFIX.sub("", option.strip())


def _normalize_options(options: object) -> list[str]:
    if options is None:
        return []
    if isinstance(options, str):
        return [_strip_option_prefix(line) for line in options.splitlines() if line.strip()]
    if not isinstance(options, list):
        return []
    normalized: list[str] = []
    for item in options:
        text = _strip_option_prefix(str(item))
        if text:
            normalized.append(text)
    return normalized


def create_ask_user_tool() -> Tool:

    def ask_user(question: str, options: list[str] | None = None) -> ToolResult:
        """缺少关键信息或需要用户确认时提问；会暂停等待回答。"""

        if not question.strip():
            return make_error_result("ask_user", "question 不能为空")

        normalized = _normalize_options(options)

        lines: list[str] = [question]
        for index, option in enumerate(normalized, start=1):
            lines.append(f"{index}. {option}")

        content = "\n".join(lines)
        data: dict[str, object] = {
            "requires_user_input": True,
            "question": question,
        }
        if normalized:
            data["options"] = normalized

        return make_text_result("ask_user", content, **data)

    return tool_from_function(ask_user)
