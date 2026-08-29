"""LoCoBench Tool → LansCoder Tool 映射。

LoCoBench 的 ``BaseAgent`` 自己执行工具(harness 只记录 ``tool_usage_log`` /
``modified_files``),LansCoder 也在自己的 loop 内执行工具。本模块把 LoCoBench
``Tool`` 的每个 ``function`` 映射为一个 LansCoder ``Tool``:

- 工具名: ``{locobench_tool.name}_{function_name}``(如 ``file_system_read_file``)。
- 参数 JSON Schema 由 LoCoBench ``ToolParameter`` 转换。
- executor 直接调用 LoCoBench 工具方法;async 方法在独立线程的临时事件循环中
  运行(executor 本身是同步回调,不能在当前运行中的 loop 里 await)。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from lanscoder.providers.types import ToolDefinition
from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result


def _parameter_schema(param: Any) -> dict[str, Any]:
    """把 LoCoBench ToolParameter 转成 OpenAI function-calling 参数 schema。"""

    schema: dict[str, Any] = {"type": param.type, "description": param.description}
    if param.enum_values:
        schema["enum"] = param.enum_values
    if param.type == "array":
        schema["items"] = {"type": "string"}
    if param.default is not None:
        schema["default"] = param.default
    return schema


def _stringify(result: Any) -> str:
    """把 LoCoBench 工具返回的任意结构转为可读文本(供 LansCoder 观察)。"""

    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(result)


def _run_async_in_thread(method: Any, kwargs: dict[str, Any]) -> Any:
    """在独立线程的新事件循环里运行 async 工具方法,并等待结果。"""

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["ok"] = True
            outcome["value"] = asyncio.run(method(**kwargs))
        except BaseException as exc:  # noqa: BLE001
            outcome["ok"] = False
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join()
    if outcome.get("ok"):
        return outcome["value"]
    raise outcome["error"]


def _execute_locobench_function(tool: Any, fn_name: str, fn: Any, kwargs: dict[str, Any]) -> ToolResult:
    """调用单个 LoCoBench 工具方法并归一化为 LansCoder ToolResult。"""

    name = f"{tool.name}_{fn_name}"
    method = getattr(tool, fn_name)
    try:
        if fn.async_function:
            result = _run_async_in_thread(method, kwargs)
        else:
            result = method(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return make_error_result(name, f"{exc}")
    return make_text_result(
        name,
        _stringify(result),
        data={"tool": tool.name, "function": fn_name},
    )


def map_locobench_tool(locobench_tool: Any) -> list[Tool]:
    """把一个 LoCoBench 工具的每个 function 映射为一个 LansCoder Tool。"""

    mapped: list[Tool] = []
    for fn_name, fn in locobench_tool.functions.items():

        def make_executor(tool: Any, name: str, func: Any) -> Any:
            def executor(**kwargs: Any) -> ToolResult:
                return _execute_locobench_function(tool, name, func, kwargs)

            return executor

        definition = ToolDefinition(
            name=f"{locobench_tool.name}_{fn_name}",
            description=fn.description,
            parameters={
                "type": "object",
                "properties": {p.name: _parameter_schema(p) for p in fn.parameters},
                "required": [p.name for p in fn.parameters if p.required],
            },
        )
        mapped.append(
            Tool(definition=definition, executor=make_executor(locobench_tool, fn_name, fn))
        )
    return mapped


def map_locobench_tools(locobench_tools: list[Any]) -> list[Tool]:
    """映射一组 LoCoBench 工具。"""

    mapped: list[Tool] = []
    for locobench_tool in locobench_tools or []:
        mapped.extend(map_locobench_tool(locobench_tool))
    return mapped
