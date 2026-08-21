"""真实 MCP stdio 与远程配置边界的集成测试。"""

from __future__ import annotations

import sys
from pathlib import Path

from lanscoder.agent.session import AgentSession
from lanscoder.context.store import JsonlSessionStore
from lanscoder.mcp.adapter import adapt_mcp_tool
from lanscoder.mcp.manager import McpManager
from lanscoder.mcp.models import McpLocalServerConfig, McpRemoteServerConfig, McpToolDescription
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import (
    PermissionAction,
    PermissionConfirmationChoice,
    PermissionDecisionKind,
)
from lanscoder.providers.types import ToolCall


def test_stdio_echo_tool_requires_confirmation_then_executes_after_explicit_allow(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    manager = McpManager(
        (
            McpLocalServerConfig(
                name="echo",
                command=(sys.executable, str(fixture)),
                timeout_ms=5_000,
            ),
        )
    )
    try:
        manager.connect_all()
        assert manager.doctor("echo").state == "connected"
        discovered = dict(manager.tools())["echo"]
        assert discovered.name == "echo"

        store = JsonlSessionStore(tmp_path / ".lanscoder")
        session = AgentSession.create(
            store=store,
            session_id="sess_mcp_echo",
            agents_md="",
            tools=[adapt_mcp_tool(manager, "echo", discovered)],
            permission_manager=PermissionManager(policy=DefaultPermissionPolicy(tmp_path)),
        )
        tool_call = ToolCall(id="call_mcp_echo", name="mcp__echo__echo", arguments={"message": "hello MCP"})

        prepared = session.permission_coordinator.prepare(tool_call, [])

        assert prepared.pending_input is not None
        assert prepared.pending_input.kind == "permission_confirmation"
        assert prepared.permission_request is not None
        assert prepared.permission_request.action == PermissionAction.MCP_TOOL
        assert prepared.permission_request.target == "echo/echo"

        allowed = session.permission_coordinator.permission_manager.resolve_confirmation(
            prepared.permission_request,
            PermissionConfirmationChoice.ALLOW_ONCE.value,
        )
        assert allowed.kind == PermissionDecisionKind.ALLOW

        result = session.execute_tool_call_after_permission_confirmation(tool_call)

        assert result.ok is True
        assert result.content == "hello MCP"
    finally:
        manager.close()


class _RemoteTransport:
    async def connect(self) -> None:
        return None

    async def list_tools(self) -> tuple[McpToolDescription, ...]:
        return (McpToolDescription("echo", "Echo text."),)

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        return {"content": [{"type": "text", "text": str(arguments["message"])}]}

    async def close(self) -> None:
        return None


class _CapturingRemoteFactory:
    def __init__(self) -> None:
        self.config: McpRemoteServerConfig | None = None

    def create(self, config: McpLocalServerConfig | McpRemoteServerConfig) -> _RemoteTransport:
        assert isinstance(config, McpRemoteServerConfig)
        self.config = config
        return _RemoteTransport()


def test_remote_config_forwards_url_and_headers_without_leaking_header_value() -> None:
    secret = "Bearer secret-value-that-must-not-appear"
    factory = _CapturingRemoteFactory()
    manager = McpManager(
        (
            McpRemoteServerConfig(
                name="remote",
                url="https://example.test/mcp",
                headers={"Authorization": secret},
            ),
        ),
        transport_factory=factory,
    )
    try:
        manager.connect_all()

        assert factory.config is not None
        assert factory.config.url == "https://example.test/mcp"
        assert factory.config.headers == {"Authorization": secret}
        status = manager.doctor("remote")
        assert status is not None
        assert status.state == "connected"
        assert secret not in repr(status)
        assert secret not in str(manager.statuses())
    finally:
        manager.close()


def test_remote_bearer_token_environment_variable_becomes_authorization_header() -> None:
    factory = _CapturingRemoteFactory()
    manager = McpManager(
        (
            McpRemoteServerConfig(
                name="remote",
                url="https://example.test/mcp",
                bearer_token_env_var="REMOTE_MCP_TOKEN",
            ),
        ),
        transport_factory=factory,
        environment={"REMOTE_MCP_TOKEN": "secret-value-that-must-not-appear"},
    )
    try:
        manager.connect_all()

        assert factory.config is not None
        assert factory.config.headers == {"Authorization": "Bearer secret-value-that-must-not-appear"}
        assert manager.doctor("remote").state == "connected"
        assert "secret-value-that-must-not-appear" not in str(manager.statuses())
    finally:
        manager.close()


def test_remote_bearer_token_environment_variable_reports_missing_variable_name() -> None:
    manager = McpManager(
        (
            McpRemoteServerConfig(
                name="remote",
                url="https://example.test/mcp",
                bearer_token_env_var="REMOTE_MCP_TOKEN",
            ),
        ),
        transport_factory=_CapturingRemoteFactory(),
        environment={},
        retry_attempts=1,
    )
    try:
        manager.connect_all()

        status = manager.doctor("remote")
        assert status.state == "failed"
        assert status.error == "缺少环境变量：REMOTE_MCP_TOKEN"
    finally:
        manager.close()
