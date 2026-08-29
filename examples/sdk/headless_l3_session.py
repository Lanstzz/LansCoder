"""Headless L3 示例(D7 / SC-7): ``create_agent_session`` 完整装配。

覆盖:
- 自定义知识工具(``tools=[...]`` 注入,替代内置工具集)。
- ``set_permission_mode``(示例用 ``bypass`` 让工具回合免交互)。
- ``tool_event_handler`` 审计:记录每个工具执行事件。
- ``resume``:同一 ``session_id`` + 同一 ``data_root`` 恢复会话并继续一轮。

运行::

    python examples/sdk/headless_l3_session.py

全程无 TUI、无网络;stub provider 返回预置回复(第一轮请求工具调用,第二轮给最终答案)。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

# 允许从仓库根目录直接运行(未安装时);已安装时这行无副作用。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lanscoder.agent.loop import ToolExecutionEvent
from lanscoder.core import create_agent_session
from lanscoder.providers.types import ChatResponse, ToolCall
from lanscoder.tools.types import Tool, ToolDefinition, make_text_result

from stub_provider import StubProvider


def lookup_knowledge_tool() -> Tool:
    """自定义知识工具:查询内置知识库(只读,无副作用)。"""

    return Tool(
        definition=ToolDefinition(
            name="lookup_knowledge",
            description="查询内置知识库,返回与 query 相关的条目。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        executor=lambda **kwargs: make_text_result(
            name="lookup_knowledge",
            content=f"知识库命中: {kwargs.get('query', '')} -> 示例答案",
        ),
    )


def audit_tool_events(event: ToolExecutionEvent) -> None:
    """tool_event_handler 审计:打印每个工具执行事件。"""

    print(f"[audit] tool_event kind={event.kind} name={event.tool_call.name}")


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="lanscoder-sdk-l3-"))
    data_root = workdir / "data"
    session_id = "sdk-headless-demo"
    tools = [lookup_knowledge_tool()]

    # 第一轮:stub 先请求调用 lookup_knowledge,再给最终答案。
    provider = StubProvider(
        replies=[
            ChatResponse(
                provider="stub",
                model="m",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_knowledge_1",
                        name="lookup_knowledge",
                        arguments={"query": "LangChain"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="stub", model="m", content="已查知识库,答案是 42。"),
        ]
    )

    handle = create_agent_session(
        provider=provider,
        project_root=workdir,
        data_root=data_root,
        tools=tools,
        session_id=session_id,
    )
    print(f"[L3] created session {handle.session.session_id}")

    # 工具回合走权限协调器;bypass 让示例免交互(自定义工具默认会被询问)。
    handle.runner.current_session.set_permission_mode("bypass")
    handle.runner.tool_event_handler = audit_tool_events

    await handle.runner.arun_user_turn("查一下 LangChain 是什么")
    print("[L3] first turn done")

    # resume:同一 session_id + 同一 data_root 恢复,再跑一轮。
    provider2 = StubProvider(
        replies=[ChatResponse(provider="stub", model="m", content="第二轮:继续对话。")]
    )
    resumed = create_agent_session(
        provider=provider2,
        project_root=workdir,
        data_root=data_root,
        tools=tools,
        session_id=session_id,
        resume=True,
    )
    await resumed.runner.arun_user_turn("继续")
    roles = [m.role for m in resumed.session.rebuild_view().messages]
    print(f"[L3] resumed session {resumed.session.session_id}; messages roles={roles}")
    assert "user" in roles and "assistant" in roles


if __name__ == "__main__":
    asyncio.run(main())
