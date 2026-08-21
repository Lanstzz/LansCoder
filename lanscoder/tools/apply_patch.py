from __future__ import annotations

from pathlib import Path

from lanscoder.permissions.types import PermissionAction
from lanscoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from lanscoder.utils.introspection import tool_from_function
from lanscoder.utils.patch import PatchOperation, PatchPlan, parse_patch
from lanscoder.utils.sandbox import PathSandbox
from lanscoder.utils.sandbox_access import SandboxAccess
from lanscoder.utils.text import safe_read_text


def create_apply_patch_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:

    sandbox = PathSandbox(root, access=access)

    def apply_patch(patch: str, dry_run: bool = False) -> ToolResult:
        """按 patch 语法新增、更新、删除或移动项目内文本文件。"""

        try:
            plan = parse_patch(patch)
            outcome = _apply_plan(sandbox, plan, dry_run=dry_run)
        except ValueError as exc:
            return make_error_result("apply_patch", str(exc))

        return make_text_result(
            "apply_patch",
            "补丁可应用。" if dry_run else "补丁已应用。",
            dry_run=dry_run,
            changed_files=outcome["changed_files"],
            created_files=outcome["created_files"],
            deleted_files=outcome["deleted_files"],
            moved_files=outcome["moved_files"],
        )

    tool = tool_from_function(apply_patch)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.WRITE_PATH,
        target_builder=_permission_target_for_patch,
        reason="应用补丁会修改项目文件，需要用户确认。",
        allow_always=False,
        allow_auto=False,
    )
    return tool


def _permission_target_for_patch(arguments: dict[str, object]) -> str:
    patch = str(arguments.get("patch") or "")
    plan = parse_patch(patch)
    files: list[str] = []
    for operation in plan.operations:
        files.append(operation.path)
        if operation.move_to:
            files.append(operation.move_to)
    return ", ".join(dict.fromkeys(files))


def _apply_plan(sandbox: PathSandbox, plan: PatchPlan, *, dry_run: bool) -> dict[str, list[str]]:

    changed_files: list[str] = []
    created_files: list[str] = []
    deleted_files: list[str] = []
    pending_writes: list[tuple[Path, str]] = []
    pending_deletes: list[Path] = []
    moved_files: list[dict[str, str]] = []

    for operation in plan.operations:
        target = sandbox.resolve(operation.path)
        relative = sandbox.relative(target)

        if operation.action == "add":
            _plan_add_file(target, operation, pending_writes)
            created_files.append(relative)
            changed_files.append(relative)
        elif operation.action == "update":
            destination = sandbox.resolve(operation.move_to) if operation.move_to else target
            _plan_update_file(target, destination, operation, pending_writes, pending_deletes)
            destination_relative = sandbox.relative(destination)
            changed_files.append(destination_relative)
            if operation.move_to:
                moved_files.append({"source": relative, "destination": destination_relative})
        elif operation.action == "delete":
            _plan_delete_file(target, pending_deletes)
            deleted_files.append(relative)
            changed_files.append(relative)
        else:
            raise ValueError(f"未知 patch 操作：{operation.action}")

    if not dry_run:
        for target, text in pending_writes:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for target in pending_deletes:
            target.unlink()

    return {
        "changed_files": changed_files,
        "created_files": created_files,
        "deleted_files": deleted_files,
        "moved_files": moved_files,
    }


def _plan_add_file(target: Path, operation: PatchOperation, pending_writes: list[tuple[Path, str]]) -> None:

    if target.exists():
        raise ValueError(f"文件已存在：{operation.path}")
    pending_writes.append((target, _join_lines(operation.add_lines)))


def _plan_update_file(
    target: Path,
    destination: Path,
    operation: PatchOperation,
    pending_writes: list[tuple[Path, str]],
    pending_deletes: list[Path],
) -> None:

    if not target.exists():
        raise ValueError(f"文件不存在：{operation.path}")
    if not target.is_file():
        raise ValueError(f"路径不是文件：{operation.path}")
    if destination != target and destination.exists():
        raise ValueError(f"目标文件已存在：{operation.move_to}")

    try:
        text = safe_read_text(target)
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 文本或无法作为文本读取：{operation.path}") from exc

    for hunk in operation.hunks:
        old_text = _join_lines(hunk.old_lines)
        new_text = _join_lines(hunk.new_lines)
        count = text.count(old_text)
        if count == 0:
            raise ValueError("没有找到要替换的内容")
        if count > 1:
            raise ValueError(f"匹配内容出现 {count} 次；请提供更精确的上下文")
        text = text.replace(old_text, new_text, 1)

    pending_writes.append((destination, text))
    if destination != target:
        pending_deletes.append(target)


def _plan_delete_file(target: Path, pending_deletes: list[Path]) -> None:

    if not target.exists():
        raise ValueError("文件不存在")
    if not target.is_file():
        raise ValueError("Delete File 只能删除文件")
    pending_deletes.append(target)


def _join_lines(lines: list[str]) -> str:

    if not lines:
        return ""
    return "\n".join(lines) + "\n"
