# tests/test_memory_store.py
from pathlib import Path

import pytest

from lanscoder.memory.models import MemoryRecord, MemoryScope
from lanscoder.memory.store import MemoryStore


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


def _memory_text(name: str) -> str:
    return f"---\nname: {name}\ndescription: d\nmetadata:\n  type: project\n---\n\nbody\n"


def _traversal_store(tmp_path: Path) -> MemoryStore:
    # Root is two levels deep so "../../escape" resolves to tmp_path/escape.md:
    # a file outside the memory root but inside the per-test directory. The
    # directories must exist for the OS to resolve the ".." traversal.
    root = tmp_path / "nested" / "mem"
    root.mkdir(parents=True, exist_ok=True)
    return MemoryStore(root, MemoryScope.PROJECT)


def test_get_traversal_name_returns_none(tmp_path: Path) -> None:
    store = _traversal_store(tmp_path)
    outside = tmp_path / "escape.md"
    outside.write_text(_memory_text("escape"), encoding="utf-8")
    assert store.get("../../escape") is None


def test_delete_traversal_name_returns_false_and_keeps_file(tmp_path: Path) -> None:
    store = _traversal_store(tmp_path)
    outside = tmp_path / "escape.md"
    outside.write_text(_memory_text("escape"), encoding="utf-8")
    assert store.delete("../../escape") is False
    assert outside.exists()


def test_exists_subdir_name_is_false(tmp_path: Path) -> None:
    assert _store(tmp_path).exists("a/b") is False
