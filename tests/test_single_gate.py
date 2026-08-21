"""§6-3 并行只读批单闸门:一次准备周期内每个工具调用 manager.preflight 恰好一次。"""

from __future__ import annotations

from lanscoder.agent.session import AgentSession
from lanscoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from lanscoder.context.store import JsonlSessionStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import PermissionDecision, PermissionRequest
from lanscoder.providers.types import ToolCall
from lanscoder.tools.types import Tool, ToolDefinition, ToolResult

BATCH_NAMES = ("view", "grep", "ls")


class _CountingPermissionManager(PermissionManager):
    """替换型 fake manager:记录 preflight 调用,求值委托真实实现。"""

    def __init__(self, *, policy) -> None:
        super().__init__(policy=policy)
        self.preflight_calls: list[PermissionRequest] = []

    def preflight(self, request: PermissionRequest) -> PermissionDecision:
        self.preflight_calls.append(request)
        return super().preflight(request)


class _CollectingEventSink:
    def __init__(self) -> None:
        self.events: list[ToolExecutionEvent] = []

    def on_tool_event(self, event: ToolExecutionEvent) -> None:
        self.events.append(event)


def _readonly_tool(name: str, execution_log: list[str]) -> Tool:
    def execute(**arguments: object) -> ToolResult:
        execution_log.append(name)
        return ToolResult(name=name, ok=True, content=f"{name}:ok")

    return Tool(
        definition=ToolDefinition(
            name=name,
            description=f"fake {name}",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        executor=execute,
    )


def _session_and_executor(
    tmp_path,
    *,
    session_id: str,
    manager: PermissionManager,
    tools: list[Tool],
) -> tuple[AgentSession, ToolExecutor, _CollectingEventSink]:
    store = JsonlSessionStore(tmp_path / ".lanscoder")
    session = AgentSession.create(
        store=store,
        session_id=session_id,
        agents_md="",
        permission_manager=manager,
        tools=tools,
    )
    sink = _CollectingEventSink()
    executor = ToolExecutor(
        session=session,
        permission_coordinator=session.permission_coordinator,
        event_sink=sink,
        cancellation_token=None,
    )
    return session, executor, sink


def _readonly_tool_calls(tmp_path, base_id: str) -> list[ToolCall]:
    return [
        ToolCall(id=f"{base_id}_0", name="view", arguments={"path": str(tmp_path / "a.txt")}),
        ToolCall(id=f"{base_id}_1", name="grep", arguments={"path": str(tmp_path)}),
        ToolCall(id=f"{base_id}_2", name="ls", arguments={"path": str(tmp_path)}),
    ]


def test_parallel_group_evaluates_each_member_exactly_once(tmp_path) -> None:
    manager = _CountingPermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    execution_log: list[str] = []
    tools = [_readonly_tool(name, execution_log) for name in BATCH_NAMES]
    _session, executor, sink = _session_and_executor(
        tmp_path,
        session_id="sess_single_gate_batch",
        manager=manager,
        tools=tools,
    )

    state = executor.execute_interactive(_readonly_tool_calls(tmp_path, "call_batch"))

    assert state.pending_input is None
    # 三个成员并入同一并行批:started 全部先行,finished 紧随其后。
    assert [event.kind for event in sink.events] == [
        "started",
        "started",
        "started",
        "finished",
        "finished",
        "finished",
    ]
    assert sorted(execution_log) == ["grep", "ls", "view"]
    # 一次准备周期内每个成员恰好一次,总数 = 成员数(不因并行扫描二次求值)。
    evaluated_names = [request.metadata["tool_name"] for request in manager.preflight_calls]
    assert evaluated_names == ["view", "grep", "ls"]


def test_single_call_is_evaluated_exactly_once(tmp_path) -> None:
    manager = _CountingPermissionManager(policy=DefaultPermissionPolicy(tmp_path))
    execution_log: list[str] = []
    tools = [_readonly_tool("view", execution_log)]
    _session, executor, sink = _session_and_executor(
        tmp_path,
        session_id="sess_single_gate_single",
        manager=manager,
        tools=tools,
    )

    state = executor.execute_interactive(
        [ToolCall(id="call_single", name="view", arguments={"path": str(tmp_path / "a.txt")})]
    )

    assert state.pending_input is None
    assert execution_log == ["view"]
    assert [event.kind for event in sink.events] == ["started", "finished"]
    assert len(manager.preflight_calls) == 1
    assert manager.preflight_calls[0].metadata["tool_name"] == "view"