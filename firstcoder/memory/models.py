"""记忆模型：MemoryScope / MemoryRecord 与 frontmatter 解析序列化。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import yaml

MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})
RESERVED_NAMES = frozenset({"memory"})
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class MemoryScope(Enum):
    USER = "user"
    PROJECT = "project"


@dataclass(slots=True)
class MemoryRecord:
    name: str
    description: str
    type: str
    body: str
    scope: MemoryScope = MemoryScope.PROJECT


def valid_name(name: str) -> bool:
    """kebab-case 且长度受限、非保留名。"""
    if not isinstance(name, str):
        return False
    if len(name) < 1 or len(name) > 64:
        return False
    if name.lower() in RESERVED_NAMES:
        return False
    return bool(_NAME_RE.fullmatch(name))


def validate_record(record: MemoryRecord) -> None:
    """写入前的强校验；非法时抛 ValueError。"""
    if not valid_name(record.name):
        raise ValueError(
            f"Invalid memory name: {record.name!r}. Use kebab-case (letters, digits, hyphens), "
            "1-64 chars, and not 'memory'."
        )
    if record.type not in MEMORY_TYPES:
        raise ValueError(
            f"Invalid memory type: {record.type!r}. Must be one of: {', '.join(sorted(MEMORY_TYPES))}."
        )
    if not record.body.strip():
        raise ValueError("Memory body must not be empty.")


def file_content(record: MemoryRecord) -> str:
    """渲染完整的记忆 markdown 文件内容。"""
    frontmatter = {
        "name": record.name,
        "description": record.description,
        "metadata": {"type": record.type},
    }
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n\n{record.body.strip()}\n"


def deserialize(text: str, scope: MemoryScope) -> MemoryRecord | None:
    """从文件文本解析记忆；frontmatter 缺失/损坏返回 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        return None
    mem_type = meta.get("metadata", {}).get("type") if isinstance(meta.get("metadata"), dict) else None
    if mem_type not in MEMORY_TYPES:
        mem_type = "reference"
    description = meta.get("description")
    if not isinstance(description, str):
        description = ""
    body = "\n".join(lines[end + 1 :]).strip()
    return MemoryRecord(name=name, description=description, type=mem_type, body=body, scope=scope)
