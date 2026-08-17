"""把记忆记录渲染成一行一条的索引文本。"""

from __future__ import annotations

from firstcoder.memory.models import MemoryRecord


class MemoryIndex:
    @staticmethod
    def render(records: list[MemoryRecord]) -> str:
        return "\n".join(
            f"- [{record.name}]({record.name}.md) — {record.description}" for record in records
        )
