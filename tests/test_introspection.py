"""从函数签名生成工具定义的测试。"""

from __future__ import annotations

import pytest

from lanscoder.utils.introspection import function_to_parameters, tool_from_function
from lanscoder.tools.types import ToolResult, make_text_result


def sample_tool(path: str, max_chars: int = 100, dry_run: bool = False, ratio: float = 0.5) -> ToolResult:
    """读取文件内容。"""

    return make_text_result("sample_tool", f"{path}:{max_chars}:{dry_run}:{ratio}")


def list_tool(paths: list[str]) -> ToolResult:
    """读取多个文件。"""

    return make_text_result("list_tool", str(paths))


def optional_list_tool(paths: list[str] | None = None) -> ToolResult:
    """可选多文件。"""

    return make_text_result("optional_list_tool", str(paths))


def optional_str_tool(name: str | None = None) -> ToolResult:
    """可选字符串。"""

    return make_text_result("optional_str_tool", str(name))


def test_function_to_parameters_uses_signature_annotations_and_defaults():
    parameters = function_to_parameters(sample_tool)

    assert parameters == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"},
            "dry_run": {"type": "boolean"},
            "ratio": {"type": "number"},
        },
        "required": ["path"],
    }


def test_tool_from_function_builds_definition_and_keeps_executor():
    tool = tool_from_function(sample_tool)

    assert tool.name == "sample_tool"
    assert tool.definition.description == "读取文件内容。"
    assert tool.definition.parameters["required"] == ["path"]
    assert tool.executor(path="README.md").content == "README.md:100:False:0.5"


def test_tool_from_function_allows_name_and_description_override():
    tool = tool_from_function(sample_tool, name="read_file", description="读取项目文件。")

    assert tool.name == "read_file"
    assert tool.definition.description == "读取项目文件。"


def test_function_to_parameters_generic_list_becomes_array_with_items():
    parameters = function_to_parameters(list_tool)

    assert parameters["properties"]["paths"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert parameters["required"] == ["paths"]


def test_function_to_parameters_optional_list_unwraps_to_array():
    parameters = function_to_parameters(optional_list_tool)

    assert parameters["properties"]["paths"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert "paths" not in parameters.get("required", [])


def test_function_to_parameters_optional_scalar_unwraps():
    parameters = function_to_parameters(optional_str_tool)

    assert parameters["properties"]["name"] == {"type": "string"}
    assert "name" not in parameters.get("required", [])


def test_function_to_parameters_rejects_args_and_kwargs():
    def bad_tool(path: str, *args: str) -> ToolResult:
        return make_text_result("bad_tool", path)

    with pytest.raises(ValueError, match="不支持可变参数"):
        function_to_parameters(bad_tool)
