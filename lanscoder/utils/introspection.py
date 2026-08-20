from __future__ import annotations

import inspect
from collections.abc import Callable
from types import UnionType
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin, get_type_hints

from lanscoder.utils.schema import object_schema, property_schema
from lanscoder.providers.types import ToolDefinition

if TYPE_CHECKING:
    from lanscoder.tools.types import Tool, ToolResult


PYTHON_TYPE_TO_JSON_TYPE: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def function_to_parameters(func: Callable[..., "ToolResult"]) -> dict[str, Any]:

    signature = inspect.signature(func)
    type_hints = get_type_hints(func)
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for parameter in signature.parameters.values():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ValueError(f"不支持可变参数：{parameter.name}")

        if parameter.name == "self":
            continue

        annotation = type_hints.get(parameter.name, parameter.annotation)
        param_schema = _annotation_to_json_schema(annotation)
        properties[parameter.name] = property_schema(
            str(param_schema["type"]),
            **{key: value for key, value in param_schema.items() if key != "type"},
        )
        if parameter.default is inspect.Signature.empty:
            required.append(parameter.name)

    return object_schema(properties, required=required)


def tool_from_function(
    func: Callable[..., "ToolResult"],
    *,
    name: str | None = None,
    description: str | None = None,
) -> "Tool":

    tool_name = name or func.__name__
    tool_description = description if description is not None else inspect.getdoc(func) or ""

    from lanscoder.tools.types import Tool

    return Tool(
        definition=ToolDefinition(
            name=tool_name,
            description=tool_description,
            parameters=function_to_parameters(func),
        ),
        executor=func,
    )


def _annotation_to_json_schema(annotation: Any) -> dict[str, Any]:

    if annotation is inspect.Signature.empty:
        return {"type": "string"}

    if annotation in PYTHON_TYPE_TO_JSON_TYPE:
        return {"type": PYTHON_TYPE_TO_JSON_TYPE[annotation]}

    origin = get_origin(annotation)
    if origin in (list, tuple):
        args = get_args(annotation)
        if args and args[0] is not Ellipsis and args[0] is not inspect.Signature.empty:
            return {"type": "array", "items": _annotation_to_json_schema(args[0])}
        return {"type": "array"}
    if origin in (Union, UnionType):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return _annotation_to_json_schema(non_none[0])
        return {"type": "string"}
    if origin in PYTHON_TYPE_TO_JSON_TYPE:
        return {"type": PYTHON_TYPE_TO_JSON_TYPE[origin]}

    return {"type": "string"}
