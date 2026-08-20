from __future__ import annotations

from lanscoder.memory.models import MemoryRecord


class MemoryIndex:
    @staticmethod
    def render(records: list[MemoryRecord]) -> str:
        return "\n".join(f"- [{record.name}]({record.name}.md) — {record.description}" for record in records)
