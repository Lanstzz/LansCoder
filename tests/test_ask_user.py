"""`ask_user` 工具测试。"""

from __future__ import annotations

from firstcoder.tools.ask_user import create_ask_user_tool


def test_ask_user_returns_question_and_input_flag():
    tool = create_ask_user_tool()

    result = tool.executor(question="请确认是否继续删除？")

    assert result.ok is True
    assert result.name == "ask_user"
    assert "请确认是否继续删除？" in result.content
    assert result.data["requires_user_input"] is True
    assert result.data["question"] == "请确认是否继续删除？"


def test_ask_user_with_options():
    tool = create_ask_user_tool()

    result = tool.executor(
        question="选择下一步操作",
        options=["继续", "跳过", "取消"],
    )

    assert result.ok is True
    assert "选择下一步操作" in result.content
    assert "1. 继续" in result.content
    assert "2. 跳过" in result.content
    assert "3. 取消" in result.content
    assert result.data["options"] == ["继续", "跳过", "取消"]


def test_ask_user_rejects_empty_question():
    tool = create_ask_user_tool()

    result = tool.executor(question="")

    assert result.ok is False
    assert "question 不能为空" in result.error


def test_ask_user_definition_has_correct_schema():
    tool = create_ask_user_tool()

    assert tool.name == "ask_user"
    assert "question" in tool.definition.parameters["properties"]
    assert tool.definition.parameters["properties"]["options"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert tool.definition.parameters["required"] == ["question"]


def test_ask_user_normalizes_multiline_string_options():
    tool = create_ask_user_tool()

    result = tool.executor(
        question="选择环境",
        options="A. 开发\nB. 生产\nC. 预发布",
    )

    assert result.ok is True
    assert "选择环境" in result.content
    assert "1. 开发" in result.content
    assert "2. 生产" in result.content
    assert "3. 预发布" in result.content
    assert result.data["options"] == ["开发", "生产", "预发布"]


def test_ask_user_normalizes_prefixed_list_options():
    tool = create_ask_user_tool()

    result = tool.executor(question="选择", options=["1. 继续", "B) 跳过", "- 取消"])

    assert result.ok is True
    assert result.data["options"] == ["继续", "跳过", "取消"]
    assert "1. 继续" in result.content
    assert "2. 跳过" in result.content
    assert "3. 取消" in result.content


def test_ask_user_ignores_non_list_options():
    tool = create_ask_user_tool()

    result = tool.executor(question="选择", options=12345)

    assert result.ok is True
    assert "options" not in result.data
    assert "1." not in result.content
