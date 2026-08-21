from __future__ import annotations

from dataclasses import dataclass, field

BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"


@dataclass(slots=True)
class PatchHunk:

    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PatchOperation:

    action: str
    path: str
    move_to: str | None = None
    add_lines: list[str] = field(default_factory=list)
    hunks: list[PatchHunk] = field(default_factory=list)


@dataclass(slots=True)
class PatchPlan:

    operations: list[PatchOperation]


def parse_patch(patch: str) -> PatchPlan:

    lines = patch.splitlines()
    if not lines or lines[0] != BEGIN_MARKER:
        raise ValueError("patch 必须以 *** Begin Patch 开头")
    if lines[-1] != END_MARKER:
        raise ValueError("patch 必须以 *** End Patch 结尾")

    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith("*** Add File: "):
            operation, index = _parse_add_file(lines, index)
        elif line.startswith("*** Update File: "):
            operation, index = _parse_update_file(lines, index)
        elif line.startswith("*** Delete File: "):
            operation, index = _parse_delete_file(lines, index)
        else:
            raise ValueError(f"无法识别的 patch 行：{line}")
        operations.append(operation)

    if not operations:
        raise ValueError("patch 中没有任何文件操作")
    return PatchPlan(operations=operations)


def _parse_add_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:

    path = lines[index].removeprefix("*** Add File: ").strip()
    if not path:
        raise ValueError("Add File 缺少路径")

    add_lines: list[str] = []
    index += 1
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        line = lines[index]
        if not line.startswith("+"):
            raise ValueError("Add File 内容行必须以 + 开头")
        add_lines.append(line[1:])
        index += 1
    return PatchOperation(action="add", path=path, add_lines=add_lines), index


def _parse_update_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:

    path = lines[index].removeprefix("*** Update File: ").strip()
    if not path:
        raise ValueError("Update File 缺少路径")

    move_to: str | None = None
    hunks: list[PatchHunk] = []
    index += 1
    while index < len(lines) - 1 and _is_update_body_line(lines[index]):
        if lines[index].startswith("*** Move to: "):
            move_to = lines[index].removeprefix("*** Move to: ").strip()
            if not move_to:
                raise ValueError("Move to 缺少路径")
            index += 1
            continue
        if not lines[index].startswith("@@"):
            raise ValueError("Update File 需要使用 @@ 开始 hunk")
        hunk, index = _parse_hunk(lines, index + 1)
        hunks.append(hunk)

    if not hunks and move_to is None:
        raise ValueError("Update File 至少需要一个 hunk")
    return PatchOperation(action="update", path=path, move_to=move_to, hunks=hunks), index


def _parse_hunk(lines: list[str], index: int) -> tuple[PatchHunk, int]:

    hunk = PatchHunk()
    while index < len(lines) - 1 and not lines[index].startswith("@@") and not lines[index].startswith("*** "):
        line = lines[index]
        if line.startswith("+"):
            hunk.new_lines.append(line[1:])
        elif line.startswith("-"):
            hunk.old_lines.append(line[1:])
        elif line.startswith(" "):
            text = line[1:]
            hunk.old_lines.append(text)
            hunk.new_lines.append(text)
        elif line == "":
            hunk.old_lines.append("")
            hunk.new_lines.append("")
        else:
            raise ValueError(f"hunk 行必须以 +、- 或空格开头：{line}")
        index += 1

    if not hunk.old_lines and not hunk.new_lines:
        raise ValueError("hunk 不能为空")
    return hunk, index


def _is_update_body_line(line: str) -> bool:

    return not line.startswith("*** ") or line.startswith("*** Move to: ")


def _parse_delete_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:

    path = lines[index].removeprefix("*** Delete File: ").strip()
    if not path:
        raise ValueError("Delete File 缺少路径")
    return PatchOperation(action="delete", path=path), index + 1