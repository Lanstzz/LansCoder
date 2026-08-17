# FirstCoder 记忆系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 FirstCoder 加入跨会话持久记忆——项目级 + 用户级，模型工具与 `/memory` 命令双写入，`MEMORY.md` 索引常驻 system prompt（参与指纹缓存），全文按需读取。

**Architecture:** 采用 Claude Code 的文件式记忆：记忆是带 YAML frontmatter 的 markdown 文件（一个记忆一个文件），存放在两个记忆根目录（用户级 `~/.firstcoder/memory/`、项目级 `{data_root}/memory/projects/{project_hash}/`）。新模块 `firstcoder/memory/` 提供 `MemoryStore` / `MemoryIndex` / `MemoryManager`；四个会话工具 `remember` / `forget` / `read_memory` / `search_memory` 注册进 `create_session_tool_registry`；记忆索引经 `SystemPromptInputs.memory_index` 注入 system prompt 指纹缓存，记忆变更自动触发前缀重建；记忆操作经 `SessionEventWriter.append_event("memory_updated", ...)` 记入会话 JSONL。

**Tech Stack:** Python 3.11+，PyYAML（已是依赖，frontmatter 解析复用 `skills/discovery.py` 的模式），pytest，Textual TUI。无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-17-memory-system-design.md`

## Global Constraints

- Python 3.11+；所有新文件使用 `from __future__ import annotations`；4 空格缩进；black line-length 200。
- 不引入新依赖（PyYAML 已存在并已在 `firstcoder/skills/discovery.py` 中使用）。
- 面向模型的工具描述使用英文（与现有工具一致）；代码内注释/文档可用中文（与仓库现状一致）。
- 测试放在扁平目录 `tests/` 下，命名 `test_*.py`，用 `tmp_path` fixture，不依赖网络/真实 API。
- 记忆根目录都位于 `git` 之外：用户级 `~/.firstcoder/memory/`，项目级 `{data_root}/memory/projects/{hash}/`（`data_root` 默认 `store.root`）。
- `name` 必须为 kebab-case（`[a-z0-9]+(-[a-z0-9]+)*`），1–64 字符，保留名 `memory`；文件名 = `<name>.md`。
- `metadata.type` ∈ `user|feedback|project|reference`；空 body / 非法 type / 非法 name 写入时拒绝。
- 记忆工具为内部工具：不设 `ToolPermissionSpec`（与 `task_create`/`load_skill` 一致），可审计性由 `memory_updated` 会话事件承担。

---

### Task 1: 记忆模型与 frontmatter（`firstcoder/memory/models.py`）

**Files:**
- Create: `firstcoder/memory/models.py`
- Test: `tests/test_memory_models.py`

**Interfaces:**
- Consumes: 无（仅依赖 stdlib + `yaml`）。
- Produces:
  - `class MemoryScope(Enum)` — `USER = "user"`、`PROJECT = "project"`。
  - `MEMORY_TYPES: frozenset[str]` = `{"user", "feedback", "project", "reference"}`。
  - `@dataclass(slots=True) class MemoryRecord` — 字段 `name: str`、`description: str`、`type: str`、`body: str`、`scope: MemoryScope = MemoryScope.PROJECT`。
  - `def valid_name(name: str) -> bool`
  - `def validate_record(record: MemoryRecord) -> None` — 非法时抛 `ValueError`。
  - `def file_content(record: MemoryRecord) -> str` — frontmatter + 空行 + body。
  - `def deserialize(text: str, scope: MemoryScope) -> MemoryRecord | None` — 坏 frontmatter 返回 `None`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_models.py
import pytest

from firstcoder.memory.models import (
    MEMORY_TYPES,
    MemoryRecord,
    MemoryScope,
    deserialize,
    file_content,
    valid_name,
    validate_record,
)


def test_memory_scope_values() -> None:
    assert MemoryScope.USER.value == "user"
    assert MemoryScope.PROJECT.value == "project"


def test_memory_types() -> None:
    assert MEMORY_TYPES == frozenset({"user", "feedback", "project", "reference"})


def test_valid_name_accepts_kebab_case() -> None:
    assert valid_name("build-commands")
    assert valid_name("a")
    assert valid_name("user-prefs-2026")


@pytest.mark.parametrize(
    "name",
    ["Build Commands", "a/b", "..", "memory", "", "x" * 65, "with space", "Über"],
)
def test_valid_name_rejects_bad_names(name: str) -> None:
    assert not valid_name(name)


def test_validate_record_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        validate_record(MemoryRecord(name="bad name", description="d", type="project", body="b"))
    with pytest.raises(ValueError):
        validate_record(MemoryRecord(name="ok-name", description="d", type="wat", body="b"))
    with pytest.raises(ValueError):
        validate_record(MemoryRecord(name="ok-name", description="d", type="project", body="   "))


def test_file_content_round_trip() -> None:
    record = MemoryRecord(
        name="build-commands",
        description="How to build and test locally",
        type="project",
        body="Run pytest.",
    )
    text = file_content(record)
    assert text.startswith("---\n")
    assert "name: build-commands" in text
    assert "description: How to build and test locally" in text
    assert "type: project" in text
    assert text.rstrip().endswith("Run pytest.")

    restored = deserialize(text, MemoryScope.PROJECT)
    assert restored is not None
    assert restored.name == "build-commands"
    assert restored.description == "How to build and test locally"
    assert restored.type == "project"
    assert restored.body == "Run pytest."
    assert restored.scope is MemoryScope.PROJECT


def test_deserialize_returns_none_for_malformed() -> None:
    assert deserialize("no frontmatter here", MemoryScope.USER) is None
    assert deserialize("---\nnot: [valid yaml\n---\nbody", MemoryScope.USER) is None
    assert deserialize("---\n---\nbody", MemoryScope.USER) is None
    assert deserialize("---\ndescription: no name\n---\nbody", MemoryScope.USER) is None


def test_deserialize_defaults_missing_type_and_description() -> None:
    restored = deserialize("---\nname: solo\n---\nbody", MemoryScope.USER)
    assert restored is not None
    assert restored.type == "reference"
    assert restored.description == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'firstcoder.memory'`

- [ ] **Step 3: 实现最小代码**

```python
# firstcoder/memory/models.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_models.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add firstcoder/memory/models.py tests/test_memory_models.py
git commit -m "Add memory model with frontmatter parse and validation"
```

---

### Task 2: 记忆索引渲染（`firstcoder/memory/index.py`）

**Files:**
- Create: `firstcoder/memory/index.py`
- Test: `tests/test_memory_index.py`

**Interfaces:**
- Consumes: `MemoryRecord`（Task 1）。
- Produces: `class MemoryIndex`，`@staticmethod render(records: list[MemoryRecord]) -> str` — 每行 `- [name](name.md) — description`，空列表返回 `""`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_index.py
from firstcoder.memory.index import MemoryIndex
from firstcoder.memory.models import MemoryRecord, MemoryScope


def test_render_empty() -> None:
    assert MemoryIndex.render([]) == ""


def test_render_one_line_per_record() -> None:
    records = [
        MemoryRecord(name="build-commands", description="How to build", type="project", body="x", scope=MemoryScope.PROJECT),
        MemoryRecord(name="user-prefs", description="Prefers Chinese", type="user", body="y", scope=MemoryScope.USER),
    ]
    assert MemoryIndex.render(records) == (
        "- [build-commands](build-commands.md) — How to build\n"
        "- [user-prefs](user-prefs.md) — Prefers Chinese"
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_index.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'firstcoder.memory.index'`

- [ ] **Step 3: 实现最小代码**

```python
# firstcoder/memory/index.py
"""把记忆记录渲染成一行一条的索引文本。"""

from __future__ import annotations

from firstcoder.memory.models import MemoryRecord


class MemoryIndex:
    @staticmethod
    def render(records: list[MemoryRecord]) -> str:
        return "\n".join(
            f"- [{record.name}]({record.name}.md) — {record.description}" for record in records
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_index.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add firstcoder/memory/index.py tests/test_memory_index.py
git commit -m "Add memory index renderer"
```

---

### Task 3: 记忆存储（`firstcoder/memory/store.py`）

**Files:**
- Create: `firstcoder/memory/store.py`
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Consumes: `MemoryRecord` / `MemoryScope` / `valid_name` / `validate_record` / `file_content` / `deserialize`（Task 1）；`MemoryIndex.render`（Task 2）。
- Produces: `@dataclass(slots=True) class MemoryStore`：
  - `__init__(self, root: Path, scope: MemoryScope) -> None`
  - `list(self) -> list[MemoryRecord]`（坏文件跳过）
  - `get(self, name: str) -> MemoryRecord | None`
  - `write(self, record: MemoryRecord) -> None`（校验、临时文件 + rename、刷新 `MEMORY.md`）
  - `delete(self, name: str) -> bool`
  - `exists(self, name: str) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_store.py
from pathlib import Path

import pytest

from firstcoder.memory.models import MemoryRecord, MemoryScope
from firstcoder.memory.store import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "mem", MemoryScope.PROJECT)


def test_list_empty_when_root_missing(tmp_path: Path) -> None:
    assert _store(tmp_path).list() == []


def test_write_creates_file_and_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(MemoryRecord(name="build-commands", description="How to build", type="project", body="Run pytest."))
    assert (store.root / "build-commands.md").exists()
    index = (store.root / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [build-commands](build-commands.md) — How to build" in index


def test_get_returns_record_with_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(MemoryRecord(name="ok-name", description="d", type="project", body="b"))
    record = store.get("ok-name")
    assert record is not None
    assert record.scope is MemoryScope.PROJECT


def test_get_missing_returns_none(tmp_path: Path) -> None:
    assert _store(tmp_path).get("nope") is None


def test_write_same_name_overwrites(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(MemoryRecord(name="ok-name", description="old", type="project", body="old body"))
    store.write(MemoryRecord(name="ok-name", description="new", type="project", body="new body"))
    record = store.get("ok-name")
    assert record is not None
    assert record.body == "new body"
    assert len(list(store.root.glob("*.md"))) == 2  # MEMORY.md + ok-name.md


def test_delete_removes_file_and_refreshes_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(MemoryRecord(name="ok-name", description="d", type="project", body="b"))
    assert store.delete("ok-name") is True
    assert not (store.root / "ok-name.md").exists()
    assert store.get("ok-name") is None
    assert store.delete("ok-name") is False


def test_list_skips_malformed_and_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(MemoryRecord(name="good-name", description="d", type="project", body="b"))
    (store.root / "broken.md").write_text("no frontmatter", encoding="utf-8")
    records = store.list()
    assert [record.name for record in records] == ["good-name"]


def test_write_rejects_invalid_record(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _store(tmp_path).write(MemoryRecord(name="bad name", description="d", type="project", body="b"))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'firstcoder.memory.store'`

- [ ] **Step 3: 实现最小代码**

```python
# firstcoder/memory/store.py
"""单个记忆根目录的文件 CRUD。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.memory.index import MemoryIndex
from firstcoder.memory.models import MemoryRecord, MemoryScope, deserialize, file_content, validate_record


class MemoryStore:
    """管理一个记忆根目录：`<name>.md` 文件 + `MEMORY.md` 索引。"""

    def __init__(self, root: Path, scope: MemoryScope) -> None:
        self.root = Path(root)
        self.scope = scope

    def _path(self, name: str) -> Path:
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
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        self._refresh_index()
        return True

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def _refresh_index(self) -> None:
        content = MemoryIndex.render(self.list())
        index_path = self.root / "MEMORY.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(content, encoding="utf-8")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add firstcoder/memory/store.py tests/test_memory_store.py
git commit -m "Add memory store with file CRUD and index refresh"
```

---

### Task 4: 记忆管理器（`firstcoder/memory/manager.py`）

**Files:**
- Create: `firstcoder/memory/manager.py`
- Test: `tests/test_memory_manager.py`

**Interfaces:**
- Consumes: `MemoryRecord` / `MemoryScope`（Task 1）；`MemoryStore`（Task 3）；`content_fingerprint`（`firstcoder/context/identity.py`）。
- Produces:
  - `def project_memory_root(data_root: Path, project_path: Path) -> Path`
  - `@dataclass(slots=True) class MemoryManager`：
    - `__init__(self, user_root: Path, project_root: Path) -> None`
    - `list(self, scope: MemoryScope) -> list[MemoryRecord]`
    - `list_all(self) -> list[MemoryRecord]`
    - `get(self, scope: MemoryScope, name: str) -> MemoryRecord | None`
    - `write(self, scope: MemoryScope, record: MemoryRecord) -> None`
    - `delete(self, scope: MemoryScope, name: str) -> bool`
    - `render_index_text(self) -> str`（用户级/项目级索引拼接，空则返回 `""`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_manager.py
from pathlib import Path

from firstcoder.context.identity import content_fingerprint
from firstcoder.memory.manager import MemoryManager, project_memory_root
from firstcoder.memory.models import MemoryRecord, MemoryScope


def test_project_memory_root_hashes_project_path(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    expected = data_root / "memory" / "projects" / content_fingerprint(str(project.resolve()))
    assert project_memory_root(data_root, project) == expected


def test_write_and_get_by_scope(tmp_path: Path) -> None:
    manager = MemoryManager(user_root=tmp_path / "user", project_root=tmp_path / "proj")
    manager.write(
        MemoryScope.PROJECT,
        MemoryRecord(name="build-commands", description="How to build", type="project", body="Run pytest."),
    )
    manager.write(
        MemoryScope.USER,
        MemoryRecord(name="user-prefs", description="Prefers Chinese", type="user", body="回答用中文。"),
    )
    assert manager.get(MemoryScope.PROJECT, "build-commands") is not None
    assert manager.get(MemoryScope.USER, "user-prefs") is not None
    assert manager.get(MemoryScope.PROJECT, "user-prefs") is None


def test_two_projects_are_isolated(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    manager_a = MemoryManager(user_root=tmp_path / "user", project_root=project_memory_root(data_root, project_a))
    manager_b = MemoryManager(user_root=tmp_path / "user", project_root=project_memory_root(data_root, project_b))
    manager_a.write(MemoryScope.PROJECT, MemoryRecord(name="only-a", description="d", type="project", body="b"))
    assert manager_b.list(MemoryScope.PROJECT) == []


def test_delete_by_scope(tmp_path: Path) -> None:
    manager = MemoryManager(user_root=tmp_path / "user", project_root=tmp_path / "proj")
    manager.write(MemoryScope.USER, MemoryRecord(name="x", description="d", type="user", body="b"))
    assert manager.delete(MemoryScope.USER, "x") is True
    assert manager.delete(MemoryScope.USER, "x") is False


def test_render_index_text_combines_scopes(tmp_path: Path) -> None:
    manager = MemoryManager(user_root=tmp_path / "user", project_root=tmp_path / "proj")
    assert manager.render_index_text() == ""
    manager.write(MemoryScope.PROJECT, MemoryRecord(name="build-commands", description="How to build", type="project", body="b"))
    manager.write(MemoryScope.USER, MemoryRecord(name="user-prefs", description="Prefers Chinese", type="user", body="y"))
    text = manager.render_index_text()
    assert "User memory:" in text
    assert "Project memory:" in text
    assert "build-commands" in text
    assert "user-prefs" in text
    assert text.index("User memory:") < text.index("Project memory:")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'firstcoder.memory.manager'`

- [ ] **Step 3: 实现最小代码**

```python
# firstcoder/memory/manager.py
"""记忆管理器：作用域解析 + 用户/项目两级存储委托。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.context.identity import content_fingerprint
from firstcoder.memory.index import MemoryIndex
from firstcoder.memory.models import MemoryRecord, MemoryScope
from firstcoder.memory.store import MemoryStore


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

    def list_all(self) -> list[MemoryRecord]:
        return self.user.list() + self.project.list()

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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_manager.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add firstcoder/memory/manager.py tests/test_memory_manager.py
git commit -m "Add memory manager with scope resolution"
```

---

### Task 5: 系统提示词接线（指纹 + Memory section）

**Files:**
- Modify: `firstcoder/context/system_prompt.py`
- Modify: `firstcoder/agent/prompt_inputs.py`
- Test: `tests/test_memory_system_prompt.py`

**Interfaces:**
- Consumes: 现有 `SystemPromptInputs` / `SystemPromptBuilder` / `content_fingerprint`。
- Produces:
  - `SystemPromptInputs.memory_index: str = ""`（新字段）。
  - `SystemPromptBuilder.fingerprint()` 把 `memory_index` 纳入计算。
  - `SystemPromptBuilder.build()` 在 memory_index 非空时输出 **Memory** section（协议文本 + 索引）。
  - `build_system_prompt_inputs(..., memory_index: str = "")` 透传字段。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_system_prompt.py
from firstcoder.agent.prompt_inputs import build_system_prompt_inputs
from firstcoder.context.system_prompt import SystemPromptBuilder, SystemPromptInputs


def _inputs(**overrides: object) -> SystemPromptInputs:
    values = {
        "base_rules": "你是 FirstCoder。",
        "agents_md": "",
        "provider_name": "openai-compatible",
        "provider_capabilities": {"tool_calling": True, "parallel_tool_calls": False},
        "permission_policy": {},
        "mode": "default",
    }
    values.update(overrides)
    return SystemPromptInputs(**values)


def test_memory_index_change_invalidates_fingerprint() -> None:
    builder = SystemPromptBuilder()
    before = builder.fingerprint(_inputs(memory_index="Project memory:\n- [a](a.md) — aaa"))
    after = builder.fingerprint(_inputs(memory_index="Project memory:\n- [b](b.md) — bbb"))
    assert before != after


def test_memory_index_stable_for_same_input() -> None:
    builder = SystemPromptBuilder()
    inputs = _inputs(memory_index="Project memory:\n- [a](a.md) — aaa")
    assert builder.fingerprint(inputs) == builder.fingerprint(inputs)


def test_empty_memory_index_omits_section() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    assert "Memory:" not in entry.messages[0].content


def test_memory_section_rendered_in_prefix() -> None:
    builder = SystemPromptBuilder()
    entry = builder.build(
        _inputs(memory_index="Project memory:\n- [build-commands](build-commands.md) — How to build")
    )
    content = entry.messages[0].content
    assert "Memory:" in content
    assert "build-commands" in content
    assert "read_memory" in content  # 协议文本提示模型按需读全文


def test_build_system_prompt_inputs_passes_memory_index() -> None:
    inputs = build_system_prompt_inputs(
        base_rules="r",
        agents_md="",
        memory_index="User memory:\n- [x](x.md) — xx",
        provider_name="openai-compatible",
    )
    assert inputs.memory_index == "User memory:\n- [x](x.md) — xx"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_system_prompt.py -q`
Expected: FAIL — `TypeError` / 断言失败（`memory_index` 字段不存在）。

- [ ] **Step 3: 实现最小代码**

在 `firstcoder/context/system_prompt.py`：

```python
MEMORY_PROTOCOL = (
    "Persistent memory is available as named files. The index above lists project-level "
    "and user-level memories. Read a full memory with read_memory before acting on it. "
    "Use remember to save durable facts: project scope for repo-specific facts, user scope "
    "for cross-project preferences."
)
```

`SystemPromptInputs` 新增字段（放在 `mode: str = "default"` 之后）：

```python
    mode: str = "default"
    memory_index: str = ""
    prompt_version: str = SYSTEM_PROMPT_VERSION
```

`fingerprint()` 新增一项（放在 `"mode"` 之后）：

```python
            "memory_index_hash": content_fingerprint(inputs.memory_index),
```

`build()` 的 section 列表新增一项（放在 `_format_section("Permission policy", ...)` 之后）：

```python
                _format_memory_section(inputs),
```

并新增模块函数：

```python
def _format_memory_section(inputs: SystemPromptInputs) -> str:
    index = inputs.memory_index.strip()
    if not index:
        return ""
    return f"Memory:\n{MEMORY_PROTOCOL}\n\n{index}"
```

在 `firstcoder/agent/prompt_inputs.py` 的 `build_system_prompt_inputs()` 签名中新增参数：

```python
    permission_policy: dict[str, Any] | None = None,
    mode: str = "default",
    memory_index: str = "",
) -> SystemPromptInputs:
```

并在返回的 `SystemPromptInputs(...)` 中传入 `memory_index=memory_index`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_system_prompt.py tests/test_context_system_prompt.py tests/test_agent_prompt_inputs.py -q`
Expected: PASS（回归旧测试仍通过）

- [ ] **Step 5: 提交**

```bash
git add firstcoder/context/system_prompt.py firstcoder/agent/prompt_inputs.py tests/test_memory_system_prompt.py
git commit -m "Wire memory index into system prompt fingerprint"
```

---

### Task 6: 记忆工具（`firstcoder/tools/memory_tools.py`）

**Files:**
- Create: `firstcoder/tools/memory_tools.py`
- Test: `tests/test_memory_tools.py`

**Interfaces:**
- Consumes: `MemoryManager`（Task 4）；`MemoryRecord` / `MemoryScope` / `validate_record`（Task 1）；`SessionEventWriter.append_event`；`object_schema`（`firstcoder/utils/schema.py`）。
- Produces: `def create_memory_tools(memory_manager: MemoryManager, writer: SessionEventWriter | None = None) -> list[Tool]`，含四个工具 `remember` / `forget` / `read_memory` / `search_memory`。成功/失败用 `make_text_result` / `make_error_result`；写操作在 `writer` 存在时追加 `memory_updated` 事件（`{"scope", "name", "action"}`，action ∈ `upsert|delete`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_tools.py
from pathlib import Path

from firstcoder.context.writer import SessionEventWriter
from firstcoder.memory.manager import MemoryManager
from firstcoder.memory.models import MemoryScope
from firstcoder.tools.memory_tools import create_memory_tools
from firstcoder.tools.registry import ToolRegistry


def _registry(tmp_path: Path) -> tuple[ToolRegistry, MemoryManager, SessionEventWriter]:
    from firstcoder.context.store import JsonlSessionStore

    store = JsonlSessionStore(tmp_path / "data")
    writer = SessionEventWriter(store=store, session_id="sess_test")
    manager = MemoryManager(user_root=tmp_path / "user", project_root=tmp_path / "proj")
    registry = ToolRegistry(create_memory_tools(manager, writer))
    return registry, manager, writer


def test_remember_writes_project_memory(tmp_path: Path) -> None:
    registry, manager, _ = _registry(tmp_path)
    result = registry.execute(
        "remember",
        {"name": "build-commands", "description": "How to build", "body": "Run pytest.", "type": "project"},
    )
    assert result.ok
    assert manager.get(MemoryScope.PROJECT, "build-commands") is not None


def test_remember_with_user_scope(tmp_path: Path) -> None:
    registry, manager, _ = _registry(tmp_path)
    registry.execute(
        "remember",
        {"name": "user-prefs", "description": "Prefers Chinese", "body": "回答用中文。", "type": "user", "scope": "user"},
    )
    assert manager.get(MemoryScope.USER, "user-prefs") is not None
    assert manager.get(MemoryScope.PROJECT, "user-prefs") is None


def test_remember_rejects_bad_scope_and_type(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    bad_scope = registry.execute("remember", {"name": "x", "description": "d", "body": "b", "scope": "nope"})
    assert not bad_scope.ok
    bad_type = registry.execute("remember", {"name": "x", "description": "d", "body": "b", "type": "wat"})
    assert not bad_type.ok


def test_forget_removes_memory(tmp_path: Path) -> None:
    registry, manager, _ = _registry(tmp_path)
    registry.execute("remember", {"name": "x", "description": "d", "body": "b"})
    result = registry.execute("forget", {"name": "x"})
    assert result.ok
    assert "Forgot" in result.content
    assert manager.get(MemoryScope.PROJECT, "x") is None


def test_forget_missing_is_not_error(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    result = registry.execute("forget", {"name": "nope"})
    assert result.ok
    assert "not found" in result.content


def test_read_memory_returns_full_content(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    registry.execute("remember", {"name": "x", "description": "d", "body": "the body text"})
    result = registry.execute("read_memory", {"name": "x"})
    assert result.ok
    assert "the body text" in result.content
    assert "x" in result.content


def test_read_memory_missing_is_not_error(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    result = registry.execute("read_memory", {"name": "nope"})
    assert result.ok
    assert "not found" in result.content


def test_search_memory_matches_body(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    registry.execute("remember", {"name": "build-commands", "description": "How to build", "body": "Run pytest."})
    registry.execute("remember", {"name": "user-prefs", "description": "Prefers Chinese", "body": "用中文", "scope": "user"})
    result = registry.execute("search_memory", {"query": "pytest"})
    assert result.ok
    assert "build-commands" in result.content
    assert "user-prefs" not in result.content
    result_all = registry.execute("search_memory", {"query": "中文", "scope": "all"})
    assert "user-prefs" in result_all.content


def test_memory_updated_event_appended(tmp_path: Path) -> None:
    registry, _, writer = _registry(tmp_path)
    registry.execute("remember", {"name": "x", "description": "d", "body": "b"})
    registry.execute("forget", {"name": "x"})
    events = writer.store.list_events(writer.session_id)
    actions = [(event.type, event.payload.get("name"), event.payload.get("action")) for event in events]
    assert ("memory_updated", "x", "upsert") in actions
    assert ("memory_updated", "x", "delete") in actions


def test_no_writer_skips_event(tmp_path: Path) -> None:
    manager = MemoryManager(user_root=tmp_path / "user", project_root=tmp_path / "proj")
    registry = ToolRegistry(create_memory_tools(manager, writer=None))
    result = registry.execute("remember", {"name": "x", "description": "d", "body": "b"})
    assert result.ok
    assert manager.get(MemoryScope.PROJECT, "x") is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'firstcoder.tools.memory_tools'`

- [ ] **Step 3: 实现最小代码**

```python
# firstcoder/tools/memory_tools.py
"""持久记忆工具：remember / forget / read_memory / search_memory。"""

from __future__ import annotations

from typing import Any

from firstcoder.context.writer import SessionEventWriter
from firstcoder.memory.manager import MemoryManager
from firstcoder.memory.models import MEMORY_TYPES, MemoryRecord, MemoryScope, validate_record
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema


def create_memory_tools(memory_manager: MemoryManager, writer: SessionEventWriter | None = None) -> list[Tool]:
    return [
        _remember_tool(memory_manager, writer),
        _forget_tool(memory_manager, writer),
        _read_memory_tool(memory_manager),
        _search_memory_tool(memory_manager),
    ]


def _resolve_scope(value: str | None) -> MemoryScope:
    return MemoryScope.PROJECT if value in (None, "") else MemoryScope(value)


def _search_scopes(scope: str) -> list[MemoryScope]:
    if scope == "all":
        return [MemoryScope.USER, MemoryScope.PROJECT]
    return [_resolve_scope(scope)]


def _append_memory_event(writer: SessionEventWriter | None, scope: MemoryScope, name: str, action: str) -> None:
    if writer is None:
        return
    writer.append_event("memory_updated", {"scope": scope.value, "name": name, "action": action})


def _remember_tool(manager: MemoryManager, writer: SessionEventWriter | None) -> Tool:
    def remember(*, name: str, description: str, body: str, type: str = "project", scope: str = "project") -> ToolResult:
        try:
            resolved_scope = _resolve_scope(scope)
            record = MemoryRecord(name=name, description=description, type=type, body=body)
            validate_record(record)
            manager.write(resolved_scope, record)
        except ValueError as exc:
            return make_error_result("remember", f"Unable to remember: {exc}")
        _append_memory_event(writer, resolved_scope, name, "upsert")
        return make_text_result(
            "remember",
            f"Saved {resolved_scope.value} memory '{name}'.",
            name=name,
            scope=resolved_scope.value,
            type=type,
        )

    parameters = _scoped_schema(
        {
            "name": {"type": "string", "description": "kebab-case slug for the memory, e.g. build-commands."},
            "description": {"type": "string", "description": "One-line summary used to decide relevance during recall."},
            "body": {"type": "string", "description": "The durable fact to remember."},
            "type": {"type": "string", "enum": sorted(MEMORY_TYPES), "default": "project"},
            "scope": {"type": "string", "enum": ["project", "user"], "default": "project"},
        },
        required=["name", "description", "body"],
    )
    return Tool(
        definition=ToolDefinition(
            name="remember",
            description=(
                "Save a durable fact to persistent memory. One memory per name; writing the "
                "same name overwrites. Use project scope for repo-specific facts and user scope "
                "for cross-project preferences."
            ),
            parameters=parameters,
        ),
        executor=remember,
    )


def _forget_tool(manager: MemoryManager, writer: SessionEventWriter | None) -> Tool:
    def forget(*, name: str, scope: str = "project") -> ToolResult:
        try:
            resolved_scope = _resolve_scope(scope)
        except ValueError as exc:
            return make_error_result("forget", f"Unable to forget: {exc}")
        deleted = manager.delete(resolved_scope, name)
        if not deleted:
            return make_text_result(
                "forget",
                f"No {resolved_scope.value} memory named '{name}' found.",
                name=name,
                scope=resolved_scope.value,
                found=False,
            )
        _append_memory_event(writer, resolved_scope, name, "delete")
        return make_text_result(
            "forget",
            f"Forgot {resolved_scope.value} memory '{name}'.",
            name=name,
            scope=resolved_scope.value,
            found=True,
        )

    parameters = _scoped_schema(
        {
            "name": {"type": "string"},
            "scope": {"type": "string", "enum": ["project", "user"], "default": "project"},
        },
        required=["name"],
    )
    return Tool(
        definition=ToolDefinition(
            name="forget",
            description="Delete a named persistent memory.",
            parameters=parameters,
        ),
        executor=forget,
    )


def _read_memory_tool(manager: MemoryManager) -> Tool:
    def read_memory(*, name: str, scope: str = "project") -> ToolResult:
        try:
            resolved_scope = _resolve_scope(scope)
        except ValueError as exc:
            return make_error_result("read_memory", f"Unable to read memory: {exc}")
        record = manager.get(resolved_scope, name)
        if record is None:
            return make_text_result(
                "read_memory",
                f"No {resolved_scope.value} memory named '{name}' found.",
                name=name,
                scope=resolved_scope.value,
                found=False,
            )
        return make_text_result(
            "read_memory",
            _render_record(record),
            name=name,
            scope=resolved_scope.value,
            type=record.type,
            found=True,
        )

    parameters = _scoped_schema(
        {
            "name": {"type": "string"},
            "scope": {"type": "string", "enum": ["project", "user"], "default": "project"},
        },
        required=["name"],
    )
    return Tool(
        definition=ToolDefinition(
            name="read_memory",
            description="Read the full content of one persistent memory by exact name.",
            parameters=parameters,
        ),
        executor=read_memory,
    )


def _search_memory_tool(manager: MemoryManager) -> Tool:
    def search_memory(*, query: str, scope: str = "project") -> ToolResult:
        try:
            scopes = _search_scopes(scope)
        except ValueError as exc:
            return make_error_result("search_memory", f"Unable to search memory: {exc}")
        needle = query.lower()
        matches: list[tuple[MemoryScope, MemoryRecord]] = []
        for candidate_scope in scopes:
            for record in manager.list(candidate_scope):
                haystack = "\n".join([record.name, record.description, record.body]).lower()
                if needle in haystack:
                    matches.append((candidate_scope, record))
        if not matches:
            return make_text_result("search_memory", f"No memories match '{query}'.", query=query, count=0)
        lines = [f"Matched {len(matches)} memories for '{query}':", ""]
        for record_scope, record in matches:
            first_line = next((line for line in record.body.splitlines() if line.strip()), "")
            lines.append(f"- [{record.name}] ({record_scope.value}) {record.description}")
            if first_line:
                lines.append(f"    {first_line}")
        return make_text_result("search_memory", "\n".join(lines), query=query, count=len(matches))

    parameters = _scoped_schema(
        {
            "query": {"type": "string", "description": "Substring to match across names, descriptions, and bodies."},
            "scope": {"type": "string", "enum": ["project", "user", "all"], "default": "project"},
        },
        required=["query"],
    )
    return Tool(
        definition=ToolDefinition(
            name="search_memory",
            description="Search persistent memory by substring across names, descriptions, and bodies.",
            parameters=parameters,
        ),
        executor=search_memory,
    )


def _render_record(record: MemoryRecord) -> str:
    return "\n".join(
        [
            f"name: {record.name}",
            f"description: {record.description}",
            f"type: {record.type}",
            "---",
            record.body,
        ]
    )


def _scoped_schema(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    schema = object_schema(properties, required=required)
    schema["additionalProperties"] = False
    return schema
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add firstcoder/tools/memory_tools.py tests/test_memory_tools.py
git commit -m "Add persistent memory tools"
```

---

### Task 7: 会话装配（registry / AgentSession / bootstrap）

**Files:**
- Modify: `firstcoder/tools/session_registry.py:42-56`
- Modify: `firstcoder/agent/session.py:73-101, 103-186, 318-347`
- Modify: `firstcoder/session/bootstrap.py:52-138`
- Test: `tests/test_memory_wiring.py`

**Interfaces:**
- Consumes: `create_memory_tools`（Task 6）；`MemoryManager` / `project_memory_root`（Task 4）。
- Produces:
  - `create_session_tool_registry(..., memory_manager: MemoryManager | None = None)` — manager 非 None 时注册记忆工具。
  - `AgentSession.memory_manager: MemoryManager | None = None`（新字段）；`create` / `resume` / `from_project` 均接受 `memory_manager: MemoryManager | None = None`；`build_system_prefix()` 传 `memory_index`。
  - `SessionBootstrap.user_memory_root: str | Path | None = None`（新字段）；`memory_manager()` 方法；`create` / `resume` / `from_project` 均传 `memory_manager=self.memory_manager()`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_wiring.py
from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.memory.manager import project_memory_root
from firstcoder.session.bootstrap import SessionBootstrap


def test_session_has_memory_tools_and_prefix(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    data_root = tmp_path / "data"
    store = JsonlSessionStore(data_root)
    bootstrap = SessionBootstrap(
        store=store,
        project_root=project_path,
        user_memory_root=tmp_path / "user-memory",
    )
    session = bootstrap.from_project()

    assert "remember" in session.tool_registry.names()
    assert "search_memory" in session.tool_registry.names()

    result = session.tool_registry.execute(
        "remember",
        {"name": "build-commands", "description": "How to build", "body": "Run pytest."},
    )
    assert result.ok
    assert (project_memory_root(data_root, project_path) / "build-commands.md").exists()

    events = store.list_events(session.session_id)
    assert any(event.type == "memory_updated" for event in events)

    prefix = session.build_system_prefix(provider_name="openai-compatible")
    assert "build-commands" in prefix[0].content
    assert "Memory:" in prefix[0].content


def test_session_without_memory_manager_has_no_memory_tools(tmp_path: Path) -> None:
    from firstcoder.agent.session import AgentSession
    from firstcoder.context.identity import new_session_id

    store = JsonlSessionStore(tmp_path / "data")
    session = AgentSession.create(store=store, session_id=new_session_id())
    assert "remember" not in session.tool_registry.names()
    prefix = session.build_system_prefix(provider_name="openai-compatible")
    assert "Memory:" not in prefix[0].content
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_wiring.py -q`
Expected: FAIL — `'remember' not in session.tool_registry.names()`（尚未接线）。

- [ ] **Step 3: 实现最小代码**

`firstcoder/tools/session_registry.py` — 新增 import 与参数，在 task-plan 工具块之后、archive 块之前注册：

```python
from firstcoder.memory.manager import MemoryManager
from firstcoder.tools.memory_tools import create_memory_tools
```

```python
    skill_catalog: SkillCatalog | None = None,
    memory_manager: MemoryManager | None = None,
) -> ToolRegistryLike:
```

```python
    if memory_manager is not None:
        for tool in create_memory_tools(memory_manager, writer):
            registry.register(tool)
```

`firstcoder/agent/session.py`：

- 类字段新增（放在 `mode: str = "default"` 之后）：

```python
    memory_manager: MemoryManager | None = None
```

- `create()` 签名新增 `memory_manager: MemoryManager | None = None`，在 `create_session_tool_registry(...)` 调用中加 `memory_manager=memory_manager`，并在 `cls(...)` 中传 `memory_manager=memory_manager`。
- `resume()` 签名与两处传递同样处理。
- `from_project()` 签名新增 `memory_manager: MemoryManager | None = None`，在内部 `cls.create(...)` 调用中传 `memory_manager=memory_manager`。
- `build_system_prefix()` 的 `build_system_prompt_inputs(...)` 调用中新增：

```python
            memory_index=self.memory_manager.render_index_text() if self.memory_manager is not None else "",
```

> 注解：`MemoryManager` 只作类型注解，`from __future__ import annotations` 下无需运行时 import。

`firstcoder/session/bootstrap.py`：

- 字段新增：`user_memory_root: str | Path | None = None`
- import：`from firstcoder.memory.manager import MemoryManager, project_memory_root`
- 新增方法：

```python
    def memory_manager(self) -> MemoryManager:
        user_root = (
            Path(self.user_memory_root)
            if self.user_memory_root is not None
            else Path.home() / ".firstcoder" / "memory"
        )
        return MemoryManager(
            user_root=user_root,
            project_root=project_memory_root(self.resolved_data_root(), Path(self.project_root)),
        )
```

- `create()` / `resume()` / `from_project()` 各自的调用中追加 `memory_manager=self.memory_manager()`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_wiring.py tests/test_agent_context_loop.py tests/test_session_resume_service.py tests/test_app_factory.py -q`
Expected: PASS（含回归）

- [ ] **Step 5: 提交**

```bash
git add firstcoder/tools/session_registry.py firstcoder/agent/session.py firstcoder/session/bootstrap.py tests/test_memory_wiring.py
git commit -m "Wire memory manager into session assembly and system prefix"
```

---

### Task 8: `/memory` 命令（TUI）

**Files:**
- Create: `firstcoder/app/memory_commands.py`
- Modify: `firstcoder/app/factory.py:280-339`
- Test: `tests/test_memory_commands.py`

**Interfaces:**
- Consumes: `MemoryManager` / `MemoryScope`（Task 4）；`MemoryIndex.render`（Task 2）；`SessionEventWriter`；`CommandResult`（`firstcoder/app/commands.py`）。
- Produces: `@dataclass(slots=True) class MemoryCommandHandler`：
  - `__init__(self, memory_provider: Callable[[], MemoryManager | None], writer_provider: Callable[[], SessionEventWriter | None] | None = None) -> None`
  - `handle(self, text: str) -> CommandResult`
  - 识别 `/memory`、`/memory remember <name>: <body>`、`/memory forget <name>`（`user:` 前缀切用户级）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_commands.py
from pathlib import Path

from firstcoder.app.commands import CommandResult
from firstcoder.app.memory_commands import MemoryCommandHandler
from firstcoder.memory.manager import MemoryManager
from firstcoder.memory.models import MemoryRecord, MemoryScope


def _handler(tmp_path: Path) -> tuple[MemoryCommandHandler, MemoryManager]:
    manager = MemoryManager(user_root=tmp_path / "user", project_root=tmp_path / "proj")
    return MemoryCommandHandler(memory_provider=lambda: manager), manager


def test_unhandled_command(tmp_path: Path) -> None:
    handler, _ = _handler(tmp_path)
    result = handler.handle("/context")
    assert not result.handled


def test_list_shows_both_scopes(tmp_path: Path) -> None:
    handler, manager = _handler(tmp_path)
    manager.write(
        MemoryScope.PROJECT,
        MemoryRecord(name="build-commands", description="How to build", type="project", body="Run pytest."),
    )
    manager.write(
        MemoryScope.USER,
        MemoryRecord(name="user-prefs", description="Prefers Chinese", type="user", body="用中文"),
    )
    result = handler.handle("/memory")
    assert result.handled
    assert "User memory:" in result.output
    assert "Project memory:" in result.output
    assert "build-commands" in result.output
    assert "user-prefs" in result.output


def test_remember_via_command(tmp_path: Path) -> None:
    handler, manager = _handler(tmp_path)
    result = handler.handle("/memory remember build-commands: Run pytest to verify")
    assert result.handled
    assert manager.get(MemoryScope.PROJECT, "build-commands") is not None


def test_forget_via_command_defaults_project(tmp_path: Path) -> None:
    handler, manager = _handler(tmp_path)
    manager.write(
        MemoryScope.PROJECT,
        MemoryRecord(name="x", description="d", type="project", body="b"),
    )
    result = handler.handle("/memory forget x")
    assert result.handled
    assert manager.get(MemoryScope.PROJECT, "x") is None


def test_forget_via_command_user_prefix(tmp_path: Path) -> None:
    handler, manager = _handler(tmp_path)
    manager.write(
        MemoryScope.USER,
        MemoryRecord(name="x", description="d", type="user", body="b"),
    )
    result = handler.handle("/memory forget user:x")
    assert result.handled
    assert manager.get(MemoryScope.USER, "x") is None


def test_no_manager_returns_message(tmp_path: Path) -> None:
    handler = MemoryCommandHandler(memory_provider=lambda: None)
    result = handler.handle("/memory")
    assert result.handled
    assert "Memory unavailable" in result.output
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_commands.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'firstcoder.app.memory_commands'`

- [ ] **Step 3: 实现最小代码**

```python
# firstcoder/app/memory_commands.py
"""/memory slash command：列出、新增、删除持久记忆。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firstcoder.app.commands import CommandResult
from firstcoder.context.writer import SessionEventWriter
from firstcoder.memory.index import MemoryIndex
from firstcoder.memory.manager import MemoryManager
from firstcoder.memory.models import MemoryRecord, MemoryScope


@dataclass(slots=True)
class MemoryCommandHandler:
    """处理 `/memory` 系列命令。"""

    memory_provider: Callable[[], MemoryManager | None]
    writer_provider: Callable[[], SessionEventWriter | None] | None = None

    def handle(self, text: str) -> CommandResult:
        command = text.strip()
        if not command.startswith("/memory"):
            return CommandResult(handled=False)
        manager = self.memory_provider()
        if manager is None:
            return CommandResult(handled=True, output="Memory unavailable: no memory manager configured")
        normalized = " ".join(command.split())
        if normalized == "/memory":
            return CommandResult(handled=True, output=_render_all(manager))
        if normalized.startswith("/memory remember "):
            return CommandResult(handled=True, output=_remember(manager, self.writer_provider, normalized))
        if normalized.startswith("/memory forget "):
            return CommandResult(handled=True, output=_forget(manager, self.writer_provider, normalized))
        return CommandResult(
            handled=True,
            output="Usage: /memory | /memory remember <name>: <body> | /memory forget [user:]<name>",
        )


def _render_all(manager: MemoryManager) -> str:
    user = manager.list(MemoryScope.USER)
    project = manager.list(MemoryScope.PROJECT)
    lines = ["User memory:"]
    lines.append(MemoryIndex.render(user) if user else "  (empty)")
    lines.append("")
    lines.append("Project memory:")
    lines.append(MemoryIndex.render(project) if project else "  (empty)")
    return "\n".join(lines)


def _remember(manager: MemoryManager, writer_provider, normalized: str) -> str:
    rest = normalized[len("/memory remember ") :]
    name, sep, body = rest.partition(":")
    name = name.strip()
    body = body.strip()
    if not sep or not name or not body:
        return "Usage: /memory remember <name>: <body>"
    try:
        manager.write(
            MemoryScope.PROJECT,
            MemoryRecord(name=name, description=_first_line(body), type="project", body=body),
        )
    except ValueError as exc:
        return f"Unable to remember: {exc}"
    _append_event(writer_provider, "project", name, "upsert")
    return f"Saved project memory '{name}'."


def _forget(manager: MemoryManager, writer_provider, normalized: str) -> str:
    arg = normalized[len("/memory forget ") :].strip()
    scope = MemoryScope.USER if arg.startswith("user:") else MemoryScope.PROJECT
    name = arg[len("user:") :].strip() if scope is MemoryScope.USER else arg
    if not name:
        return "Usage: /memory forget [user:]<name>"
    if not manager.delete(scope, name):
        return f"No {scope.value} memory named '{name}' found."
    _append_event(writer_provider, scope.value, name, "delete")
    return f"Forgot {scope.value} memory '{name}'."


def _first_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()[:80]
    return body[:80]


def _append_event(writer_provider, scope: str, name: str, action: str) -> None:
    if writer_provider is None:
        return
    writer = writer_provider()
    if writer is not None:
        writer.append_event("memory_updated", {"scope": scope, "name": name, "action": action})
```

`firstcoder/app/factory.py` — 新增 import 与 handler，并把 handler 加入 `CompositeCommandHandler` 列表：

```python
from firstcoder.app.memory_commands import MemoryCommandHandler
```

在 `skill_handler` 之后构造：

```python
    memory_handler = MemoryCommandHandler(
        memory_provider=lambda: current.session.memory_manager,
        writer_provider=lambda: current.session.writer,
    )
```

把 `memory_handler` 追加进 `CompositeCommandHandler([...])` 列表（放在 `skill_handler` 之后）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_commands.py tests/test_app_tui.py tests/test_app_factory.py -q`
Expected: PASS（含回归）

- [ ] **Step 5: 提交**

```bash
git add firstcoder/app/memory_commands.py firstcoder/app/factory.py tests/test_memory_commands.py
git commit -m "Add /memory slash command"
```

---

### Task 9: 端到端集成测试 + 全量回归

**Files:**
- Test: `tests/test_memory_e2e.py`

**Interfaces:**
- Consumes: `SessionBootstrap`（Task 7）、记忆工具（Task 6）、`SystemPromptBuilder`（Task 5）。

- [ ] **Step 1: 写集成测试**

```python
# tests/test_memory_e2e.py
"""记忆系统端到端验证：remember → 落盘 → 事件 → 前缀重建。"""

from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.memory.manager import project_memory_root
from firstcoder.session.bootstrap import SessionBootstrap


def test_memory_lifecycle_end_to_end(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    data_root = tmp_path / "data"
    user_memory_root = tmp_path / "user-memory"
    store = JsonlSessionStore(data_root)

    # 第一次会话：写一条项目记忆
    bootstrap = SessionBootstrap(
        store=store,
        project_root=project_path,
        user_memory_root=user_memory_root,
    )
    session = bootstrap.from_project()
    result = session.tool_registry.execute(
        "remember",
        {"name": "build-commands", "description": "How to build", "body": "Run pytest."},
    )
    assert result.ok

    memory_file = project_memory_root(data_root, project_path) / "build-commands.md"
    assert memory_file.read_text(encoding="utf-8").startswith("---\n")
    index_file = memory_file.parent / "MEMORY.md"
    assert "build-commands" in index_file.read_text(encoding="utf-8")

    # 前缀包含索引
    prefix = session.build_system_prefix(provider_name="openai-compatible")
    assert "build-commands" in prefix[0].content

    # 事件可审计
    events = store.list_events(session.session_id)
    assert any(event.type == "memory_updated" for event in events)

    # 第二次会话（resume）：记忆仍在，可读取、可搜索、可删除
    resumed = bootstrap.resume(session.session_id)
    assert "remember" in resumed.tool_registry.names()

    read_result = resumed.tool_registry.execute("read_memory", {"name": "build-commands"})
    assert read_result.ok
    assert "Run pytest." in read_result.content

    search_result = resumed.tool_registry.execute("search_memory", {"query": "pytest"})
    assert "build-commands" in search_result.content

    forget_result = resumed.tool_registry.execute("forget", {"name": "build-commands"})
    assert forget_result.ok
    assert not memory_file.exists()
```

- [ ] **Step 2: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_e2e.py -q`
Expected: PASS

- [ ] **Step 3: 运行全量测试套件**

Run: `.venv/bin/python -m pytest -q`
Expected: 全绿（新增记忆测试 + 既有回归）。

- [ ] **Step 4: 提交**

```bash
git add tests/test_memory_e2e.py
git commit -m "Add end-to-end memory lifecycle test"
```
