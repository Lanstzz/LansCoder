"""工具层 registry 和通用行为测试。"""

from __future__ import annotations

import pytest

from lanscoder.providers.types import ToolDefinition
from lanscoder.tools import ToolRegistry, create_builtin_registry
from lanscoder.tools.edit import create_edit_tool
from lanscoder.tools.fetch import create_fetch_tool
from lanscoder.tools.glob import create_glob_tool
from lanscoder.tools.grep import create_grep_tool
from lanscoder.tools.ls import create_ls_tool
from lanscoder.tools.tree import create_tree_tool
from lanscoder.tools.view import create_view_tool
from lanscoder.tools.write import create_write_tool
from lanscoder.tools.delete import create_delete_tool
from lanscoder.tools.apply_patch import create_apply_patch_tool
from lanscoder.tools.diagnostics import create_diagnostics_tool
from lanscoder.tools.python_exec import create_python_exec_tool
from lanscoder.tools.shell import create_shell_tool
from lanscoder.tools.task_boundary import create_task_boundary_tool
from lanscoder.tools.think import create_think_tool
from lanscoder.tools.read_multi import create_read_multi_tool
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.tools.session_registry import create_session_tool_registry
from lanscoder.tools.task_create import create_task_create_tool
from lanscoder.tools.task_list import create_task_list_tool
from lanscoder.tools.task_revise import create_task_revise_tool
from lanscoder.tools.task_update import create_task_update_tool
from lanscoder.tools.types import Tool, make_text_result
from lanscoder.tools.git_log import create_git_log_tool
from lanscoder.tools.git_diff import create_git_diff_tool
from lanscoder.tools.git_status import create_git_status_tool
from lanscoder.tools.web_search import create_web_search_tool


def test_builtin_tool_descriptions_are_agent_facing_english(tmp_path):
    registry = create_builtin_registry(
        tmp_path,
        include_mutation_tools=True,
        include_execution_tools=True,
        include_network_tools=True,
    )
    descriptions = {definition.name: definition.description for definition in registry.definitions()}

    assert descriptions["view"].startswith("Read a UTF-8 text file")
    assert "Use this instead of shell commands like cat" in descriptions["view"]
    assert descriptions["grep"].startswith("Search file contents")
    assert "literal text" in descriptions["grep"]
    assert descriptions["apply_patch"].startswith("Apply a structured patch")
    assert "Prefer this for multi-file edits" in descriptions["apply_patch"]
    assert descriptions["shell"].startswith("Run a shell command")
    assert "Prefer dedicated tools" in descriptions["shell"]
    assert "evidence returned to the model" in descriptions["diagnostics"]


def test_builtin_registry_contains_read_only_tools(tmp_path):
    registry = create_builtin_registry(tmp_path)

    assert registry.names() == [
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
        "think",
        "read_multi",
        "ask_user",
    ]
    assert [definition.name for definition in registry.definitions()] == registry.names()
    assert [tool.name for tool in registry.tools()] == registry.names()


def test_each_tool_has_its_own_module():
    assert create_ls_tool.__module__ == "lanscoder.tools.ls"
    assert create_view_tool.__module__ == "lanscoder.tools.view"
    assert create_grep_tool.__module__ == "lanscoder.tools.grep"
    assert create_glob_tool.__module__ == "lanscoder.tools.glob"
    assert create_tree_tool.__module__ == "lanscoder.tools.tree"
    assert create_git_status_tool.__module__ == "lanscoder.tools.git_status"
    assert create_git_diff_tool.__module__ == "lanscoder.tools.git_diff"
    assert create_write_tool.__module__ == "lanscoder.tools.write"
    assert create_edit_tool.__module__ == "lanscoder.tools.edit"
    assert create_delete_tool.__module__ == "lanscoder.tools.delete"
    assert create_fetch_tool.__module__ == "lanscoder.tools.fetch"
    assert create_web_search_tool.__module__ == "lanscoder.tools.web_search"
    assert create_apply_patch_tool.__module__ == "lanscoder.tools.apply_patch"
    assert create_diagnostics_tool.__module__ == "lanscoder.tools.diagnostics"
    assert create_python_exec_tool.__module__ == "lanscoder.tools.python_exec"
    assert create_shell_tool.__module__ == "lanscoder.tools.shell"
    assert create_task_boundary_tool.__module__ == "lanscoder.tools.task_boundary"
    assert create_think_tool.__module__ == "lanscoder.tools.think"
    assert create_read_multi_tool.__module__ == "lanscoder.tools.read_multi"
    assert create_ask_user_tool.__module__ == "lanscoder.tools.ask_user"
    assert create_task_create_tool.__module__ == "lanscoder.tools.task_create"
    assert create_task_update_tool.__module__ == "lanscoder.tools.task_update"
    assert create_task_revise_tool.__module__ == "lanscoder.tools.task_revise"
    assert create_task_list_tool.__module__ == "lanscoder.tools.task_list"
    assert create_git_log_tool.__module__ == "lanscoder.tools.git_log"


def test_builtin_registry_can_include_mutation_tools_when_explicitly_enabled(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    assert registry.names() == [
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
        "think",
        "read_multi",
        "ask_user",
        "write",
        "edit",
        "delete",
        "apply_patch",
    ]


def test_builtin_registry_can_include_network_tools_when_explicitly_enabled(tmp_path):
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    assert registry.names() == [
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
        "think",
        "read_multi",
        "ask_user",
        "fetch",
        "web_search",
    ]


def test_builtin_registry_can_include_execution_tools_when_explicitly_enabled(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    assert registry.names() == [
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
        "think",
        "read_multi",
        "ask_user",
        "shell",
        "python_exec",
    ]


def test_builtin_tool_definitions_are_generated_from_function_signatures(tmp_path):
    registry = create_builtin_registry(tmp_path)
    definitions = {definition.name: definition for definition in registry.definitions()}

    assert definitions["view"].parameters == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    }
    assert definitions["grep"].parameters["required"] == ["pattern"]
    assert definitions["glob"].parameters["required"] == ["pattern"]


def test_registry_returns_error_for_unknown_tool():
    registry = ToolRegistry()

    result = registry.execute("missing_tool", {})

    assert result.ok is False
    assert result.error == "未知工具：missing_tool"


def test_session_registry_adds_four_authoritative_task_plan_tools(tmp_path):
    from lanscoder.context.store import JsonlSessionStore
    from lanscoder.context.writer import SessionEventWriter

    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_plan")
    registry = create_session_tool_registry(
        session_id="sess_plan",
        archive_root=tmp_path,
        store=store,
        writer=writer,
    )

    for name in ("task_create", "task_update", "task_revise", "task_list", "load_skill"):
        assert name in registry.names()
    descriptions = {definition.name: definition.description for definition in registry.definitions()}
    assert "stable task ID" in descriptions["task_update"]
    assert "wording" in descriptions["task_revise"]


@pytest.mark.parametrize(
    "reserved_name",
    ["task_create", "task_update", "task_revise", "task_list", "retrieve_archive", "load_skill"],
)
def test_session_registry_rejects_supplied_task_plan_tool_override(
    tmp_path,
    reserved_name: str,
) -> None:
    supplied = Tool(
        definition=ToolDefinition(
            name=reserved_name,
            description="fake",
            parameters={"type": "object", "properties": {}},
        ),
        executor=lambda: make_text_result(reserved_name, "fake"),
    )

    with pytest.raises(ValueError, match="reserved"):
        create_session_tool_registry(
            session_id="sess_plan",
            tools=[supplied],
            archive_root=tmp_path,
        )
