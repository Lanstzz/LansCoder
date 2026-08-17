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
