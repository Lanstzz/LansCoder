"""外置权限分类表:classify/build_request 的逐字转写锁。

断言值来自工具文件现存的 ToolPermissionSpec/permission_registry.py
算法(Task 8 才删除),与 classification.py 表条目互为防漂移锁。
"""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from lanscoder.permissions.classification import build_request, classify
from lanscoder.permissions.types import PermissionAction, PermissionRequest
from lanscoder.providers.types import ToolDefinition
from lanscoder.tools.apply_patch import _permission_target_for_patch
from lanscoder.tools.path_permissions import read_multi_target, read_path_target
from lanscoder.tools.permission_registry import permission_request_for_tool
from lanscoder.tools.python_exec import _permission_target_for_python_exec
from lanscoder.tools.types import Tool, ToolPermissionSpec, make_text_result
from lanscoder.tools.web_search import EXA_MCP_URL, PARALLEL_MCP_URL

GATED = [
    "write",
    "delete",
    "shell",
    "edit",
    "apply_patch",
    "fetch",
    "git_diff",
    "git_status",
    "git_log",
    "diagnostics",
    "python_exec",
    "web_search",
    "grep",
    "glob",
    "ls",
    "tree",
    "view",
    "read_multi",
]

PATCH_SAMPLE = "*** Begin Patch\n*** Add File: a.txt\n+hello world\n*** End Patch"
PATCH_MOVE = (
    "*** Begin Patch\n"
    "*** Update File: b.txt\n"
    "*** Move to: c.txt\n"
    "@@\n"
    "-old\n"
    "+new\n"
    "*** End Patch"
)


@pytest.mark.parametrize(
    ("name", "action", "reason", "allow_auto", "allow_always"),
    [
        ("write", PermissionAction.WRITE_PATH, "写入文件需要用户确认。", True, True),
        ("delete", PermissionAction.DELETE_PATH, "删除路径需要用户确认。", True, True),
        ("shell", PermissionAction.EXECUTE_SHELL, "执行 shell 命令需要用户确认。", True, True),
        ("edit", PermissionAction.WRITE_PATH, "编辑文件需要用户确认。", True, True),
        ("apply_patch", PermissionAction.WRITE_PATH, "应用补丁会修改项目文件，需要用户确认。", False, False),
        ("fetch", PermissionAction.NETWORK_REQUEST, "网络请求需要用户确认。", True, True),
        ("git_diff", PermissionAction.GIT_OPERATION, "查看 git diff 属于 git 操作。", True, True),
        ("git_status", PermissionAction.GIT_OPERATION, "查看 git 状态属于 git 操作。", True, True),
        ("git_log", PermissionAction.GIT_OPERATION, "查看 git log 属于 git 操作。", True, True),
        ("diagnostics", PermissionAction.EXECUTE_SHELL, "运行诊断命令需要用户确认。", True, True),
        ("python_exec", PermissionAction.EXECUTE_SHELL, "执行 Python 代码需要用户确认。", False, False),
        ("web_search", PermissionAction.NETWORK_REQUEST, "网页搜索需要网络请求权限。", True, True),
        ("grep", PermissionAction.READ_PATH, "搜索文件内容需要权限检查。", True, True),
        ("glob", PermissionAction.READ_PATH, "匹配路径需要权限检查。", True, True),
        ("ls", PermissionAction.READ_PATH, "列出目录需要权限检查。", True, True),
        ("tree", PermissionAction.READ_PATH, "读取目录树需要权限检查。", True, True),
        ("view", PermissionAction.READ_PATH, "读取文件需要权限检查。", True, True),
        ("read_multi", PermissionAction.READ_PATH, "批量读取文件需要权限检查。", True, True),
    ],
)
def test_gated_tool_spec_matches_tool_file_declaration(
    name: str, action: PermissionAction, reason: str, allow_auto: bool, allow_always: bool
) -> None:
    spec = classify(name, {})
    assert spec is not None
    assert spec.action == action
    assert spec.reason == reason
    assert spec.allow_auto is allow_auto
    assert spec.allow_always is allow_always


def test_apply_patch_missing_patch_raises_begin_marker_error() -> None:
    with pytest.raises(ValueError, match="patch 必须以 \\*\\*\\* Begin Patch 开头"):
        build_request("apply_patch", {})


def test_shell_missing_command_raises_missing_target_error() -> None:
    with pytest.raises(ValueError, match="权限声明缺少目标参数：command"):
        build_request("shell", {})


def test_git_status_target_is_fixed_value() -> None:
    request = build_request("git_status", {})
    assert request.target == "status --short"
    assert request.action == PermissionAction.GIT_OPERATION


@pytest.mark.parametrize(
    ("name", "expected_target", "expected_reason"),
    [
        ("mcp__server__tool", "server/tool", "调用 MCP 工具 server/tool。"),
        ("mcp__a__b__c", "a__b/c", "调用 MCP 工具 a__b/c。"),
        ("mcp__srv__my_tool", "srv/my_tool", "调用 MCP 工具 srv/my_tool。"),
    ],
)
def test_mcp_prefixed_tool_classifies_as_mcp_tool(
    name: str, expected_target: str, expected_reason: str
) -> None:
    spec = classify(name, {})
    assert spec is not None
    assert spec.action == PermissionAction.MCP_TOOL
    assert spec.target_value == expected_target
    assert spec.reason == expected_reason
    assert spec.allow_auto is False
    request = build_request(name, {})
    assert request.action == PermissionAction.MCP_TOOL
    assert request.target == expected_target
    assert request.reason == expected_reason


def test_unknown_tool_name_classifies_none_and_build_raises() -> None:
    assert classify("no_such_tool", {}) is None
    with pytest.raises(ValueError, match="工具没有权限声明：no_such_tool"):
        build_request("no_such_tool", {})


def test_gated_name_hits_by_name_without_tool_object() -> None:
    """F-8:同名自定义工具名也按名命中,不因对象无 permission 而豁免。"""
    spec = classify("shell", {})
    assert spec is not None
    assert spec.action == PermissionAction.EXECUTE_SHELL
    request = build_request("shell", {"command": "pytest"})
    assert request.target == "pytest"
    assert request.reason == "执行 shell 命令需要用户确认。"


def test_write_build_request_fields() -> None:
    request = build_request("write", {"path": "a.txt"})
    assert isinstance(request, PermissionRequest)
    assert request.id.startswith("perm_write_")
    assert len(request.id) == len("perm_write_") + 12
    assert all(char in "0123456789abcdef" for char in request.id[len("perm_write_"):])
    assert request.action == PermissionAction.WRITE_PATH
    assert request.target == "a.txt"
    assert request.reason == "写入文件需要用户确认。"
    assert request.cwd is None
    assert set(request.metadata) == {"tool_name", "arguments", "allow_always", "allow_auto"}
    assert request.metadata["tool_name"] == "write"
    assert request.metadata["arguments"] == {"path": "a.txt"}
    assert request.metadata["allow_auto"] is True
    assert request.metadata["allow_always"] is True


def test_web_search_target_is_literal_url_pair() -> None:
    expected = f"{EXA_MCP_URL},{PARALLEL_MCP_URL}"
    spec = classify("web_search", {})
    assert spec is not None
    assert spec.target_value == expected
    assert build_request("web_search", {}).target == expected


def test_read_path_target_matches_tool_file_builder() -> None:
    assert build_request("view", {"path": "src"}).target == read_path_target({"path": "src"})
    assert build_request("view", {}).target == read_path_target({})
    assert build_request("grep", {"path": "pkg"}).target == read_path_target({"path": "pkg"})
    assert build_request("read_multi", {"paths": ["a", "b"]}).target == read_multi_target({"paths": ["a", "b"]})
    assert build_request("read_multi", {"paths": "a"}).target == read_multi_target({"paths": "a"})


def test_patch_python_exec_git_diff_targets_match_tool_file_builders() -> None:
    assert build_request("apply_patch", {"patch": PATCH_SAMPLE}).target == _permission_target_for_patch(
        {"patch": PATCH_SAMPLE}
    )
    assert build_request("apply_patch", {"patch": PATCH_MOVE}).target == _permission_target_for_patch(
        {"patch": PATCH_MOVE}
    )
    assert build_request("python_exec", {"code": "print(1)"}).target == _permission_target_for_python_exec(
        {"code": "print(1)"}
    )
    long_code = "x = 1\n" * 100
    assert build_request("python_exec", {"code": long_code}).target == _permission_target_for_python_exec(
        {"code": long_code}
    )
    assert build_request("git_diff", {"staged": True}).target == "diff --cached"
    assert build_request("git_diff", {}).target == "diff"


def _tool_with_spec(name: str, spec) -> Tool:
    return Tool(
        definition=ToolDefinition(name=name, description="", parameters={}),
        executor=lambda **_kwargs: make_text_result(name, "ok"),
        permission=ToolPermissionSpec(
            action=spec.action,
            target_arg=spec.target_arg,
            target_value=spec.target_value,
            cwd_arg=spec.cwd_arg,
            reason=spec.reason,
            allow_always=spec.allow_always,
            allow_auto=spec.allow_auto,
        ),
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("write", {"path": "a.txt", "content": "hello"}),
        ("shell", {"command": "pytest", "cwd": "pkg"}),
        ("git_status", {}),
        ("web_search", {"query": "python"}),
        ("fetch", {"url": "https://example.com"}),
    ],
)
def test_build_request_matches_current_permission_request_for_tool(name: str, arguments: dict) -> None:
    spec = classify(name, arguments)
    assert spec is not None
    expected = permission_request_for_tool(_tool_with_spec(name, spec), dict(arguments))
    actual = build_request(name, dict(arguments))
    assert actual == expected


def test_request_id_is_stable_for_argument_order() -> None:
    first = build_request("write", {"path": "a.txt", "content": "hello"})
    second = build_request("write", {"content": "hello", "path": "a.txt"})
    assert first.id == second.id
    payload = json.dumps(
        {"tool": "write", "arguments": {"path": "a.txt", "content": "hello"}},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    assert first.id == f"perm_write_{sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def test_classify_signature_accepts_arguments_positionally() -> None:
    args = {"path": "x"}
    spec = classify("view", args)
    assert spec is not None