"""外置权限分类表:按工具名分类权限声明并构建 PermissionRequest。

分类表与 target/cwd/request_id 算法为 tools/permission_registry.py +
各工具文件 ToolPermissionSpec 的逐字转写(Task 8/9 删除源对象);本模块
禁止 import lanscoder.tools / lanscoder.agent / lanscoder.app。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from lanscoder.permissions.types import PermissionAction, PermissionRequest
from lanscoder.utils.patch import parse_patch


@dataclass(frozen=True, slots=True)
class ClassificationSpec:

    action: PermissionAction
    target_arg: str | None = None
    target_value: str | None = None
    target_builder: Callable[[dict[str, Any]], str] | None = None
    cwd_arg: str | None = None
    reason: str = ""
    allow_always: bool = True
    allow_auto: bool = True


def _read_path_target(arguments: dict[str, Any]) -> str:
    return str(arguments.get("path") or ".")


def _read_multi_target(arguments: dict[str, Any]) -> str:
    paths = arguments.get("paths")
    if not isinstance(paths, list):
        return ""
    return "\n".join(str(path) for path in paths)


def _patch_files_target(arguments: dict[str, object]) -> str:
    patch = str(arguments.get("patch") or "")
    plan = parse_patch(patch)
    files: list[str] = []
    for operation in plan.operations:
        files.append(operation.path)
        if operation.move_to:
            files.append(operation.move_to)
    return ", ".join(dict.fromkeys(files))


def _python_exec_target(arguments: dict[str, object]) -> str:
    code = str(arguments.get("code") or "")
    preview = code if len(code) <= 200 else code[:200] + "..."
    return f"python -c {preview}"


def _git_diff_target(arguments: dict[str, object]) -> str:
    return "diff --cached" if bool(arguments.get("staged")) else "diff"


_CLASSIFICATION: dict[str, ClassificationSpec] = {
    "write": ClassificationSpec(
        action=PermissionAction.WRITE_PATH,
        target_arg="path",
        reason="写入文件需要用户确认。",
    ),
    "delete": ClassificationSpec(
        action=PermissionAction.DELETE_PATH,
        target_arg="path",
        reason="删除路径需要用户确认。",
    ),
    "shell": ClassificationSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_arg="command",
        cwd_arg="cwd",
        reason="执行 shell 命令需要用户确认。",
    ),
    "edit": ClassificationSpec(
        action=PermissionAction.WRITE_PATH,
        target_arg="path",
        reason="编辑文件需要用户确认。",
    ),
    "apply_patch": ClassificationSpec(
        action=PermissionAction.WRITE_PATH,
        target_builder=_patch_files_target,
        reason="应用补丁会修改项目文件，需要用户确认。",
        allow_always=False,
        allow_auto=False,
    ),
    "fetch": ClassificationSpec(
        action=PermissionAction.NETWORK_REQUEST,
        target_arg="url",
        reason="网络请求需要用户确认。",
    ),
    "git_diff": ClassificationSpec(
        action=PermissionAction.GIT_OPERATION,
        target_builder=_git_diff_target,
        reason="查看 git diff 属于 git 操作。",
    ),
    "git_status": ClassificationSpec(
        action=PermissionAction.GIT_OPERATION,
        target_value="status --short",
        reason="查看 git 状态属于 git 操作。",
    ),
    "git_log": ClassificationSpec(
        action=PermissionAction.GIT_OPERATION,
        target_value="log",
        reason="查看 git log 属于 git 操作。",
    ),
    "diagnostics": ClassificationSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_arg="command",
        reason="运行诊断命令需要用户确认。",
    ),
    "python_exec": ClassificationSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_builder=_python_exec_target,
        cwd_arg="cwd",
        reason="执行 Python 代码需要用户确认。",
        allow_always=False,
        allow_auto=False,
    ),
    # target 为 tools/web_search.py 的 EXA_MCP_URL 与 PARALLEL_MCP_URL 原样拼接;
    # 表禁止 import tools,故内联字面量,由 tests/test_classification.py 属性测试锁住防漂移。
    "web_search": ClassificationSpec(
        action=PermissionAction.NETWORK_REQUEST,
        target_value="https://mcp.exa.ai/mcp,https://search.parallel.ai/mcp",
        reason="网页搜索需要网络请求权限。",
    ),
    "grep": ClassificationSpec(
        action=PermissionAction.READ_PATH,
        target_builder=_read_path_target,
        reason="搜索文件内容需要权限检查。",
    ),
    "glob": ClassificationSpec(
        action=PermissionAction.READ_PATH,
        target_builder=_read_path_target,
        reason="匹配路径需要权限检查。",
    ),
    "ls": ClassificationSpec(
        action=PermissionAction.READ_PATH,
        target_builder=_read_path_target,
        reason="列出目录需要权限检查。",
    ),
    "tree": ClassificationSpec(
        action=PermissionAction.READ_PATH,
        target_builder=_read_path_target,
        reason="读取目录树需要权限检查。",
    ),
    "view": ClassificationSpec(
        action=PermissionAction.READ_PATH,
        target_builder=_read_path_target,
        reason="读取文件需要权限检查。",
    ),
    "read_multi": ClassificationSpec(
        action=PermissionAction.READ_PATH,
        target_builder=_read_multi_target,
        reason="批量读取文件需要权限检查。",
    ),
}


def _lookup(tool_name: str) -> ClassificationSpec | None:
    spec = _CLASSIFICATION.get(tool_name)
    if spec is not None:
        return spec
    if tool_name.startswith("mcp__"):
        parts = tool_name.removeprefix("mcp__").rsplit("__", 1)
        if len(parts) != 2:
            return None
        server, tool = parts
        return ClassificationSpec(
            action=PermissionAction.MCP_TOOL,
            target_value=f"{server}/{tool}",
            reason=f"调用 MCP 工具 {server}/{tool}。",
            allow_auto=False,
        )
    return None


def classify(tool_name: str, arguments: dict) -> ClassificationSpec | None:
    return _lookup(tool_name)


def _spec_target(spec: ClassificationSpec, arguments: dict) -> str:
    if spec.target_builder is not None:
        return spec.target_builder(arguments)
    if spec.target_value is not None:
        return spec.target_value
    if spec.target_arg is None:
        return ""
    if spec.target_arg not in arguments:
        raise ValueError(f"权限声明缺少目标参数：{spec.target_arg}")
    return str(arguments[spec.target_arg])


def _spec_cwd(spec: ClassificationSpec, arguments: dict) -> Path | None:
    if spec.cwd_arg is None:
        return None
    raw = arguments.get(spec.cwd_arg)
    if raw in (None, ""):
        return None
    return Path(str(raw))


def _permission_request_id(tool_name: str, arguments: dict) -> str:
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return f"perm_{tool_name}_{sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def build_request(tool_name: str, arguments: dict) -> PermissionRequest:
    spec = _lookup(tool_name)
    if spec is None:
        raise ValueError(f"工具没有权限声明：{tool_name}")
    target = _spec_target(spec, arguments)
    cwd = _spec_cwd(spec, arguments)
    request_id = _permission_request_id(tool_name, arguments)
    return PermissionRequest(
        id=request_id,
        action=spec.action,
        target=target,
        reason=spec.reason or f"工具 {tool_name} 请求 {spec.action.value} 权限。",
        cwd=cwd,
        metadata={
            "tool_name": tool_name,
            "arguments": dict(arguments),
            "allow_always": spec.allow_always,
            "allow_auto": spec.allow_auto,
        },
    )