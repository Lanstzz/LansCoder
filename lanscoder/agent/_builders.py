"""AgentLoop 的唯一构造路径。

所有创建 AgentLoop 的调用方都走 ``create_agent_loop``，在这里集中解析默认依赖并注入
协作对象。后续任务（工具注册、TurnObserver、PermissionCoordinator 等）在此函数内逐步
构造更多协作对象；最终由组装根调用它完成装配。
"""

from __future__ import annotations

from lanscoder.agent.loop import AgentLoop
from lanscoder.agent.request_builder import RequestBuilder
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.providers.types import MainRequestOptions


def create_agent_loop(**kwargs) -> AgentLoop:
    request_builder = RequestBuilder(
        session=kwargs["session"],
        provider=kwargs.get("provider"),
        context_builder=kwargs.get("context_builder") or ContextBuilder(),
        request_options=kwargs.get("request_options") or MainRequestOptions(),
        context_window=kwargs.get("context_window"),
    )
    return AgentLoop(**kwargs, request_builder=request_builder)
