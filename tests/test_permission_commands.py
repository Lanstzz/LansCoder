from lanscoder.agent.session import AgentSession
from lanscoder.app.permission_commands import PermissionCommandHandler
from lanscoder.core.runtime import CurrentSessionState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.permissions.types import PermissionMode
from lanscoder.providers.types import ToolCall
from lanscoder.tools.builtin import create_builtin_registry
from lanscoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


def test_permission_mode_command_shows_current_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=CurrentSessionState(session))

    result = handler.handle("/mode")

    assert result.handled is True
    assert "Permission mode: standard" in result.output
    assert "Available: standard, aggressive, bypass" in result.output
    assert "conservative" not in result.output


def test_permission_mode_command_updates_session_and_manager(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=CurrentSessionState(session))

    result = handler.handle("/mode aggressive")

    assert result.handled is True
    assert result.output == "Permission mode set to: aggressive"
    assert session.permission_mode == PermissionMode.AGGRESSIVE.value
    assert session.permission_coordinator.permission_manager is not None
    assert session.permission_coordinator.permission_manager.mode == PermissionMode.AGGRESSIVE


def test_permission_mode_command_accepts_bypass(tmp_path) -> None:
    access = SandboxAccess()
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=create_builtin_registry(tmp_path, access=access).tools(),
        sandbox_access=access,
    )
    handler = PermissionCommandHandler(session=CurrentSessionState(session))

    result = handler.handle("/mode bypass")

    assert result.handled is True
    assert result.output == "Permission mode set to: bypass"
    assert session.permission_mode == PermissionMode.BYPASS.value
    assert session.permission_coordinator.permission_manager is not None
    assert session.permission_coordinator.permission_manager.mode == PermissionMode.BYPASS
    assert access.mode == SandboxAccessMode.UNRESTRICTED
    assert session.permission_coordinator.permission_policy["path_access"] == "unrestricted"


def test_permission_mode_command_restores_project_sandbox_after_bypass(tmp_path) -> None:
    access = SandboxAccess(SandboxAccessMode.UNRESTRICTED)
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=create_builtin_registry(tmp_path, access=access).tools(),
        sandbox_access=access,
    )
    session.permission_coordinator.set_mode(PermissionMode.BYPASS)
    handler = PermissionCommandHandler(session=CurrentSessionState(session))

    result = handler.handle("/mode standard")

    assert result.handled is True
    assert session.permission_mode == PermissionMode.STANDARD.value
    assert access.mode == SandboxAccessMode.PROJECT
    assert session.permission_coordinator.permission_policy["path_access"] == "project_root_only"


def test_bypass_mode_lets_existing_tools_access_outside_project(tmp_path) -> None:
    access = SandboxAccess()
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=create_builtin_registry(tmp_path, access=access).tools(),
        sandbox_access=access,
    )
    tool_call = ToolCall(id="call_view", name="view", arguments={"path": str(outside)})

    confirmation = session.permission_coordinator.prepare(tool_call, [])
    session.permission_coordinator.set_mode(PermissionMode.BYPASS)
    bypass = session.permission_coordinator.prepare(tool_call, [])
    allowed = session.tool_registry.execute("view", {"path": str(outside)})
    session.permission_coordinator.set_mode(PermissionMode.STANDARD)
    confirmation_again = session.permission_coordinator.prepare(tool_call, [])

    assert confirmation.pending_input is not None
    assert confirmation.pending_input.kind == "permission_confirmation"
    assert confirmation.permission_request is not None
    assert confirmation.permission_request.action.value == "read_path"
    assert bypass.pending_input is None
    assert allowed.ok is True
    assert "secret" in allowed.content
    assert confirmation_again.pending_input is not None
    assert confirmation_again.pending_input.kind == "permission_confirmation"
    assert confirmation_again.permission_request is not None
    assert confirmation_again.permission_request.action.value == "read_path"


def test_permission_mode_command_rejects_unknown_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=CurrentSessionState(session))

    result = handler.handle("/mode chaos")

    assert result.handled is True
    assert "Unknown permission mode" in result.output
    assert session.permission_mode == PermissionMode.STANDARD.value


def test_permission_mode_command_rejects_removed_conservative_mode(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".lanscoder"),
        session_id="sess_mode",
        project_root=tmp_path,
        tools=[],
    )
    handler = PermissionCommandHandler(session=CurrentSessionState(session))

    result = handler.handle("/mode conservative")

    assert result.handled is True
    assert result.output == "Unknown permission mode. Available: standard, aggressive, bypass"
    assert session.permission_mode == PermissionMode.STANDARD.value


def test_permission_mode_enum_contains_only_three_modes() -> None:
    assert [mode.value for mode in PermissionMode] == ["standard", "aggressive", "bypass"]
