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
