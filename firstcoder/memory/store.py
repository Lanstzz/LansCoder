"""单个记忆根目录的文件 CRUD。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.memory.index import MemoryIndex
from firstcoder.memory.models import MemoryRecord, MemoryScope, deserialize, file_content, valid_name, validate_record


class MemoryStore:
    """管理一个记忆根目录：`<name>.md` 文件 + `MEMORY.md` 索引。"""

    def __init__(self, root: Path, scope: MemoryScope) -> None:
        self.root = Path(root)
        self.scope = scope

    def _path(self, name: str) -> Path:
        if not valid_name(name):
            raise ValueError(f"Invalid memory name: {name!r}")
        return self.root / f"{name}.md"

    def list(self) -> list[MemoryRecord]:
        if not self.root.exists():
            return []
        records: list[MemoryRecord] = []
        for path in sorted(self.root.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            record = deserialize(text, self.scope)
            if record is not None:
                records.append(record)
        return records

    def get(self, name: str) -> MemoryRecord | None:
        if not valid_name(name):
            return None
        path = self._path(name)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return deserialize(text, self.scope)

    def write(self, record: MemoryRecord) -> None:
        validate_record(record)
        record.scope = self.scope
        path = self._path(record.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(file_content(record), encoding="utf-8")
        temp.replace(path)
        self._refresh_index()

    def delete(self, name: str) -> bool:
        if not valid_name(name):
            return False
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        self._refresh_index()
        return True

    def exists(self, name: str) -> bool:
        if not valid_name(name):
            return False
        return self._path(name).exists()

    def _refresh_index(self) -> None:
        content = MemoryIndex.render(self.list())
        index_path = self.root / "MEMORY.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(content, encoding="utf-8")
