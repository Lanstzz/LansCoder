from firstcoder.agent.session import AgentSession
from firstcoder.app.permission_commands import PermissionCommandHandler
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.types import PermissionMode


def test_permission_mode_command_shows_current_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode")

    assert result.handled is True
    assert "Permission mode: standard" in result.output


def test_permission_mode_command_updates_session_and_manager(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode aggressive")

    assert result.handled is True
    assert result.output == "Permission mode set to: aggressive"
    assert session.mode == PermissionMode.AGGRESSIVE.value
    assert session.permission_manager is not None
    assert session.permission_manager.mode == PermissionMode.AGGRESSIVE


def test_permission_mode_command_rejects_unknown_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=session)

    result = handler.handle("/mode chaos")

    assert result.handled is True
    assert "Unknown permission mode" in result.output
    assert session.mode == PermissionMode.STANDARD.value
