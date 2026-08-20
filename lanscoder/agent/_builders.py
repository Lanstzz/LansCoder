"""AgentLoop 的唯一构造路径。

所有创建 AgentLoop 的调用方都走 ``create_agent_loop``，在这里集中解析默认依赖并注入
协作对象。工具注册也在组装根完成（``register_loop_tools``），AgentLoop 构造时不再注册
任何工具；后续任务（TurnObserver、PermissionCoordinator 等）在此函数内逐步构造更多协作
对象，最终由组装根调用它完成装配。
"""

from __future__ import annotations

from lanscoder.agent.loop import AgentLoop
from lanscoder.agent.request_builder import RequestBuilder
from lanscoder.agent.subagent import SubagentRunner
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.providers.types import MainRequestOptions
from lanscoder.tools.background import create_background_cancel_tool, create_background_status_tool
from lanscoder.tools.delegate import create_delegate_tool


def register_loop_tools(
    session,
    *,
    caller_tools,
    background_manager,
    provider,
    request_options,
    subagent_runner=None,
) -> SubagentRunner | None:
    """在组装根注册 caller / 后台控制 / delegate 工具，返回供 loop 注入的 SubagentRunner。

    ``AgentLoop`` 构造时不再注册任何工具，registry 是唯一的工具来源。同名工具跳过注册
    （去重）；delegate 注册前先构造 SubagentRunner，其 child 工具快照不含 delegate，防止
    递归委托。
    """

    registry = session.tool_registry
    for tool in caller_tools or []:
        if tool.name not in registry.names():
            registry.register(tool)
    if background_manager is not None:
        if "background_status" not in registry.names():
            registry.register(create_background_status_tool(background_manager, session_id=session.session_id))
        if "background_cancel" not in registry.names():
            registry.register(create_background_cancel_tool(background_manager, session_id=session.session_id))
    if subagent_runner is None:
        project_root = session.permission_manager.policy.project_root if session.permission_manager is not None else None
        subagent_runner = SubagentRunner(
            store=session.store,
            provider=provider,
            tools=[t for t in registry.tools() if t.name != "delegate"],
            project_root=project_root,
            agents_md=session.agents_md,
            skill_catalog=session.skill_catalog,
            permission_manager=session.permission_manager,
            sandbox_access=session.sandbox_access,
            request_options=request_options,
            background_manager=background_manager,
        )
    if "delegate" not in registry.names():
        registry.register(create_delegate_tool(subagent_runner, parent_session_id=session.session_id))
    return subagent_runner


def create_agent_loop(**kwargs) -> AgentLoop:
    request_builder = RequestBuilder(
        session=kwargs["session"],
        provider=kwargs.get("provider"),
        context_builder=kwargs.get("context_builder") or ContextBuilder(),
        request_options=kwargs.get("request_options") or MainRequestOptions(),
        context_window=kwargs.get("context_window"),
    )
    runner = register_loop_tools(
        kwargs["session"],
        caller_tools=kwargs.get("tools"),
        background_manager=kwargs.get("background_manager"),
        provider=kwargs.get("provider"),
        request_options=kwargs.get("request_options"),
    )
    return AgentLoop(**kwargs, request_builder=request_builder, subagent_runner=runner)
