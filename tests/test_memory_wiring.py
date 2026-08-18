from pathlib import Path

from lanscoder.context.store import JsonlSessionStore
from lanscoder.memory.manager import project_memory_root
from lanscoder.session.bootstrap import SessionBootstrap


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
    from lanscoder.agent.session import AgentSession
    from lanscoder.context.identity import new_session_id

    store = JsonlSessionStore(tmp_path / "data")
    session = AgentSession.create(store=store, session_id=new_session_id())
    assert "remember" not in session.tool_registry.names()
    prefix = session.build_system_prefix(provider_name="openai-compatible")
    assert "Memory:" not in prefix[0].content
