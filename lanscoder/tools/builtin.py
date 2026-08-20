from __future__ import annotations

from pathlib import Path

from lanscoder.tools.apply_patch import create_apply_patch_tool
from lanscoder.tools.ask_user import create_ask_user_tool
from lanscoder.tools.edit import create_edit_tool
from lanscoder.tools.delete import create_delete_tool
from lanscoder.tools.diagnostics import create_diagnostics_tool
from lanscoder.tools.git_diff import create_git_diff_tool
from lanscoder.tools.git_log import create_git_log_tool
from lanscoder.tools.git_status import create_git_status_tool
from lanscoder.tools.fetch import create_fetch_tool
from lanscoder.tools.glob import create_glob_tool
from lanscoder.tools.grep import create_grep_tool
from lanscoder.tools.ls import create_ls_tool
from lanscoder.tools.python_exec import create_python_exec_tool
from lanscoder.tools.read_multi import create_read_multi_tool
from lanscoder.tools.registry import ToolRegistry
from lanscoder.tools.think import create_think_tool
from lanscoder.tools.shell import create_shell_tool
from lanscoder.tools.tree import create_tree_tool
from lanscoder.tools.view import create_view_tool
from lanscoder.tools.web_search import create_web_search_tool
from lanscoder.tools.write import create_write_tool
from lanscoder.tools.descriptions import apply_agent_tool_description
from lanscoder.utils.sandbox_access import SandboxAccess


def create_builtin_registry(
    root: str | Path,
    include_mutation_tools: bool = False,
    include_execution_tools: bool = False,
    include_network_tools: bool = False,
    access: SandboxAccess | None = None,
) -> ToolRegistry:

    tools = [
        create_ls_tool(root, access=access),
        create_view_tool(root, access=access),
        create_grep_tool(root, access=access),
        create_glob_tool(root, access=access),
        create_tree_tool(root, access=access),
        create_git_status_tool(root, access=access),
        create_git_diff_tool(root, access=access),
        create_git_log_tool(root, access=access),
        create_diagnostics_tool(root, access=access),
        create_think_tool(),
        create_read_multi_tool(root, access=access),
        create_ask_user_tool(),
    ]
    if include_mutation_tools:
        tools.extend(
            [
                create_write_tool(root, access=access),
                create_edit_tool(root, access=access),
                create_delete_tool(root, access=access),
                create_apply_patch_tool(root, access=access),
            ]
        )
    if include_execution_tools:
        tools.extend(
            [
                create_shell_tool(root, access=access),
                create_python_exec_tool(root, access=access),
            ]
        )
    if include_network_tools:
        tools.extend(
            [
                create_fetch_tool(),
                create_web_search_tool(),
            ]
        )
    return ToolRegistry([apply_agent_tool_description(tool) for tool in tools])
