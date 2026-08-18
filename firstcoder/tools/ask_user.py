"""`ask_user` 工具。

当模型遇到歧义、需要确认、或缺少关键信息时，主动向用户提问。
返回的结果包含 `requires_user_input` 标记，后续 agent 主循环可以识别这个标记
并暂停执行，等待用户回答后再继续。

这是骨架阶段的实现：工具本身只负责生成标准化的问题请求，
真正的"暂停并等待用户输入"由上层 agent 主循环和 UI 负责处理。
"""

from __future__ import annotations

import re

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function

_OPTION_PREFIX = re.compile(r"^(?:[-*•]|\d+[.)、]|[A-Za-z][.)、])\s*")


def _strip_option_prefix(option: str) -> str:
    """去掉选项前导的序号/符号前缀，如 `1. `、`A) `、`- `、`• `。"""
    return _OPTION_PREFIX.sub("", option.strip())


def _normalize_options(options: object) -> list[str]:
    """把模型传入的 options 规整为字符串列表。

    有的模型会把 `options` 传成多行字符串（如 `"A. xxx\nB. yyy"`）而非列表，
    这里做防御性归一：字符串按行拆分并去掉序号前缀，其他非法输入忽略。
    """
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
    """创建向用户提问的工具。"""

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
