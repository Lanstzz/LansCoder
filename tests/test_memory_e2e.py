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
