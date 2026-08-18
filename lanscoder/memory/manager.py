# lanscoder/memory/manager.py
"""记忆管理器：作用域解析 + 用户/项目两级存储委托。"""

from __future__ import annotations

from pathlib import Path

from lanscoder.context.identity import content_fingerprint
from lanscoder.memory.index import MemoryIndex
from lanscoder.memory.models import MemoryRecord, MemoryScope
from lanscoder.memory.store import MemoryStore


def project_memory_root(data_root: Path, project_path: Path) -> Path:
    """项目记忆根目录：按项目绝对路径的稳定哈希隔离。"""
    return data_root / "memory" / "projects" / content_fingerprint(str(project_path.resolve()))


class MemoryManager:
    """同时管理用户级与项目级两个记忆存储。"""

    def __init__(self, user_root: Path, project_root: Path) -> None:
        self.user = MemoryStore(user_root, MemoryScope.USER)
        self.project = MemoryStore(project_root, MemoryScope.PROJECT)

    def store(self, scope: MemoryScope) -> MemoryStore:
        if scope is MemoryScope.USER:
            return self.user
        return self.project

    def list(self, scope: MemoryScope) -> list[MemoryRecord]:
        return self.store(scope).list()

    def get(self, scope: MemoryScope, name: str) -> MemoryRecord | None:
        return self.store(scope).get(name)

    def write(self, scope: MemoryScope, record: MemoryRecord) -> None:
        self.store(scope).write(record)

    def delete(self, scope: MemoryScope, name: str) -> bool:
        return self.store(scope).delete(name)

    def render_index_text(self) -> str:
        sections = []
        user_records = self.user.list()
        if user_records:
            sections.append(f"User memory:\n{MemoryIndex.render(user_records)}")
        project_records = self.project.list()
        if project_records:
            sections.append(f"Project memory:\n{MemoryIndex.render(project_records)}")
        return "\n\n".join(sections)
