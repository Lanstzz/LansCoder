import pytest

from lanscoder.agent.session import AgentSession
from lanscoder.context.store import JsonlSessionStore
from lanscoder.permissions.classification import build_request
from lanscoder.permissions.grants import PermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import (
    PermissionAction,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionRequest,
    PermissionScopeType,
)
from lanscoder.providers.types import ToolCall, ToolDefinition
from lanscoder.tools.registry import ToolRegistry
from lanscoder.tools.types import Tool, make_text_result


def _write_tool(calls: list[dict[str, object]] | None = None) -> Tool:
    def executor(path: str, content: str = ""):
        if calls is not None:
            calls.append({"path": path, "content": content})
        return make_text_result("write", f"wrote {path}")

    return Tool(
        definition=ToolDefinition(
            name="write",
            description="写文件。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path"],
            },
        ),
        executor=executor,
    )


def _plain_tool(calls: list[str]) -> Tool:
    def executor(text: str):
        calls.append(text)
        return make_text_result("echo", text)

    return Tool(
        definition=ToolDefinition(
            name="echo",
            description="回显。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        executor=executor,
    )


class _CountingPermissionManager(PermissionManager):
    """替换型 fake manager:记录 preflight 调用,求值委托真实实现。"""

    def __init__(self, *, policy) -> None:
        super().__init__(policy=policy)
        self.preflight_calls: list[PermissionRequest] = []

    def preflight(self, request: PermissionRequest) -> PermissionDecision:
        self.preflight_calls.append(request)
        return super().preflight(request)


def _coordinator_session(tmp_path, manager, tools=None) -> AgentSession:
    """装配 coordinator 路径的会话,并把注册表换成纯 ToolRegistry(验证闸门不依赖 registry 类型)。"""
    store = JsonlSessionStore(tmp_path / ".lanscoder")
    session = AgentSession.create(
        store=store,
        session_id="sess_coordinator_gate",
        agents_md="",
        permission_manager=manager,
    )
    session.tool_registry = ToolRegistry(list(tools or []))
    return session


def test_plain_tool_prepare_is_ungated_and_executes(tmp_path) -> None:
    calls: list[str] = []
    session = _coordinator_session(tmp_path, PermissionManager(policy=DefaultPermissionPolicy(tmp_path)), [_plain_tool(calls)])
    tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hi"})

    prepared = session.permission_coordinator.prepare(tool_call, [])

    assert prepared.pending_input is None
    assert prepared.permission_request is None
    result = session.execute_tool_call(tool_call)
    assert result.ok is True
    assert result.content == "hi"
    assert calls == ["hi"]


def test_write_returns_confirmation_without_executing(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    session = _coordinator_session(tmp_path, PermissionManager(policy=DefaultPermissionPolicy(tmp_path)), [_write_tool(calls)])
    tool_call = ToolCall(id="call_write", name="write", arguments={"path": "README.md", "content": "hello"})

    prepared = session.permission_coordinator.prepare(tool_call, [])

    assert prepared.pending_input is not None
    assert prepared.pending_input.kind == "permission_confirmation"
    assert prepared.permission_request is not None
    assert prepared.permission_request.action == PermissionAction.WRITE_PATH
    assert prepared.permission_request.target == "README.md"
    assert prepared.permission_request.cwd == tmp_path.resolve()
    assert calls == []


def test_delete_outside_root_returns_denied_result(tmp_path) -> None:
    session = _coordinator_session(tmp_path, PermissionManager(policy=DefaultPermissionPolicy(tmp_path)))

    prepared = session.permission_coordinator.prepare(
        ToolCall(id="call_del", name="delete", arguments={"path": "../outside.txt"}),
        [],
    )

    assert prepared.pending_input is None
    assert prepared.result is not None
    assert prepared.result.data["request_type"] == "permission_denied"
    assert prepared.permission_request is not None
    assert prepared.permission_request.action == PermissionAction.DELETE_PATH


def test_write_executes_after_matching_grant(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    grant = PermissionGrant(
        id="grant_write_readme",
        effect="allow",
        action=PermissionAction.WRITE_PATH,
        scope_type=PermissionScopeType.EXACT_PATH,
        scope_value=str((tmp_path / "README.md").resolve()),
        created_at="2026-06-04T00:00:00+08:00",
    )
    manager = PermissionManager(policy=DefaultPermissionPolicy(tmp_path), grants=PermissionGrantStore([grant]))
    session = _coordinator_session(tmp_path, manager, [_write_tool(calls)])
    session.require_prewrite_review = False
    tool_call = ToolCall(id="call_write", name="write", arguments={"path": "README.md", "content": "hello"})

    preflight = session.permission_coordinator.preflight(tool_call)
    assert preflight is not None
    assert preflight.decision.kind == PermissionDecisionKind.ALLOW

    result = session.execute_tool_call_after_permission_confirmation(tool_call)

    assert result.ok is True
    assert calls == [{"path": "README.md", "content": "hello"}]


def test_build_request_reports_missing_target_argument() -> None:
    try:
        build_request("write", {"content": "hello"})
    except ValueError as exc:
        assert "path" in str(exc)
    else:
        raise AssertionError("expected missing target argument error")


def test_prepare_missing_target_argument_does_not_execute(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    session = _coordinator_session(tmp_path, PermissionManager(policy=DefaultPermissionPolicy(tmp_path)), [_write_tool(calls)])

    prepared = session.permission_coordinator.prepare(
        ToolCall(id="call_write", name="write", arguments={"content": "hello"}),
        [],
    )

    assert prepared.result is not None
    assert "path" in (prepared.result.error or "")
    assert calls == []


def test_build_request_id_is_stable_for_argument_order() -> None:
    first = build_request("write", {"path": "README.md", "content": "hello"})
    second = build_request("write", {"content": "hello", "path": "README.md"})

    assert first.id == second.id


def test_coordinator_normalizes_relative_cwd_arg(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    session = _coordinator_session(tmp_path, PermissionManager(policy=DefaultPermissionPolicy(tmp_path)))

    prepared = session.permission_coordinator.prepare(
        ToolCall(id="call_shell", name="shell", arguments={"command": "pytest", "cwd": "pkg"}),
        [],
    )

    assert prepared.pending_input is not None
    assert prepared.permission_request is not None
    assert prepared.permission_request.cwd == (tmp_path / "pkg").resolve()


@pytest.mark.parametrize(
    ("name", "arguments", "expected_reason"),
    [
        ("write", {"content": "hello"}, "权限声明缺少目标参数：path"),
        ("delete", {"recursive": True}, "权限声明缺少目标参数：path"),
        ("edit", {"old": "a", "new": "b"}, "权限声明缺少目标参数：path"),
        ("shell", {"flag": "-l"}, "权限声明缺少目标参数：command"),
        ("diagnostics", {}, "权限声明缺少目标参数：command"),
        ("fetch", {"method": "GET"}, "权限声明缺少目标参数：url"),
        ("apply_patch", {}, "patch 必须以 *** Begin Patch 开头"),
    ],
)
def test_prepare_missing_argument_short_circuits_deny_without_preflight(
    tmp_path, name: str, arguments: dict, expected_reason: str
) -> None:
    """F-2 短路:缺参 → build_request 抛 ValueError → coordinator 直接回 DENY,绝不调 manager.preflight。"""

    manager = _CountingPermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    session = _coordinator_session(tmp_path, manager)
    prepared = session.permission_coordinator.prepare(
        ToolCall(id=f"call_{name}", name=name, arguments=arguments),
        [],
    )

    assert manager.preflight_calls == []
    assert prepared.permission_request is not None
    assert prepared.permission_request.id == f"perm_{name}_invalid"
    assert prepared.permission_request.target == ""
    assert prepared.permission_request.reason == expected_reason
    assert prepared.result is not None
    assert prepared.result.ok is False
    assert prepared.result.data["request_type"] == "permission_denied"
    assert prepared.result.data["permission_decision"] == "deny"


def test_preflight_invalid_arguments_deny_decision_never_reaches_manager(tmp_path) -> None:
    """缺参请求在 preflight 层即回 DENY decision,request.id 固定为 perm_{name}_invalid。"""

    manager = _CountingPermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    session = _coordinator_session(tmp_path, manager)
    preflight = session.permission_coordinator.preflight(
        ToolCall(id="call_write", name="write", arguments={})
    )

    assert manager.preflight_calls == []
    assert preflight is not None
    assert preflight.decision.kind is PermissionDecisionKind.DENY
    assert preflight.decision.reason == "权限声明缺少目标参数：path"
    assert preflight.request.id == "perm_write_invalid"
    assert preflight.request.target == ""
    assert preflight.request.reason == "权限声明缺少目标参数：path"


def test_coordinator_preflight_unknown_mcp_segment_is_ungated(tmp_path) -> None:
    """I-1:单段 mcp__foo 解析为 None → preflight 不放行也不抛,直接不门控(loop 回合不被打崩)。"""

    manager = _CountingPermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    session = _coordinator_session(tmp_path, manager)
    prepared = session.permission_coordinator.prepare(
        ToolCall(id="call_mcp", name="mcp__foo", arguments={}),
        [],
    )

    assert manager.preflight_calls == []
    assert prepared.permission_request is None
    assert prepared.pending_input is None
    assert prepared.result is None


def test_coordinator_gates_tool_by_name_with_plain_registry(tmp_path) -> None:
    """单闸门:会话注册表换成纯 ToolRegistry 后,write 仍按名 ASK(闸门不依赖 registry 类型)。"""

    manager = _CountingPermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    session = _coordinator_session(tmp_path, manager)

    prepared = session.permission_coordinator.prepare(
        ToolCall(id="call_write", name="write", arguments={"path": "README.md", "content": "hello"}),
        [],
    )

    assert len(manager.preflight_calls) == 1
    assert prepared.pending_input is not None
    assert prepared.pending_input.kind == "permission_confirmation"