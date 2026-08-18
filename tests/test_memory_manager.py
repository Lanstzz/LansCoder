# tests/test_memory_manager.py
from pathlib import Path

from lanscoder.context.identity import content_fingerprint
from lanscoder.memory.manager import MemoryManager, project_memory_root
from lanscoder.memory.models import MemoryRecord, MemoryScope


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
