"""工具注册表:按名称登记与查询工具,执行调用并把异常统一转换为 ToolResult。"""

from __future__ import annotations

from typing import Any

from lanscoder.providers.types import ToolDefinition
from lanscoder.tools.types import Tool, ToolResult, make_error_result


class ToolRegistry:
    """按名称管理工具并执行调用,把异常统一转换为 ToolResult。"""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        """初始化注册表并登记初始工具。"""
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """登记工具,重名时抛错。"""

        if tool.name in self._tools:
            raise ValueError(f"工具已存在：{tool.name}")
        self._tools[tool.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        """返回全部工具定义。"""

        return [tool.definition for tool in self._tools.values()]

    def names(self) -> list[str]:
        """返回全部工具名。"""

        return list(self._tools.keys())

    def tools(self) -> list[Tool]:
        """返回全部工具对象。"""

        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        """按名取工具,不存在返回 None。"""

        return self._tools.get(name)

    def execute(self, name: str, arguments: dict[str, Any] | str | None = None) -> ToolResult:
        """执行工具,把参数与执行异常统一转换为错误 ToolResult。"""

        tool = self._tools.get(name)
        if tool is None:
            return make_error_result(name, f"未知工具：{name}")

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return make_error_result(name, "工具参数不是合法对象", raw_arguments=arguments)

        try:
            return tool.executor(**arguments)
        except TypeError as exc:
            return make_error_result(name, f"工具参数错误：{exc}", arguments=arguments)
        except Exception as exc:  # noqa: BLE001
            return make_error_result(name, f"工具执行失败：{exc}", arguments=arguments)
