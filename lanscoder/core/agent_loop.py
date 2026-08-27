"""L1 无状态裸循环(O1=方案 A)。

公开面只接收 prompts / LoopContext / LoopConfig / signal,事件以
``AsyncIterator[AgentEvent]`` 外推;不暴露 AgentSession、不落持久化、不 import TUI。
内部用临时目录的内存会话适配现有会话绑定引擎(`create_agent_loop` + `AgentLoop`)。
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from lanscoder.agent.session import AgentSession
from lanscoder.agent.tool_execution import ToolExecutionEvent
from lanscoder.context.models import AgentMessage
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.types import ChatStreamEvent
from lanscoder.utils.cancellation import CancellationToken
from lanscoder.utils.sandbox_access import SandboxAccess

from lanscoder.core.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from lanscoder.core.messages import LoopContext, LoopConfig, LoopMessage
from lanscoder.core.runtime import create_agent_loop


def _agent_message_to_loop(message: AgentMessage) -> LoopMessage:
    """把会话消息压平成 LoopMessage(text 部分拼接,保留 id/时间元数据)。"""

    text = "\n".join(
        part.content for part in message.parts if part.kind in ("text", "tool_result")
    )
    return LoopMessage(
        role=message.role,
        content=text,
        metadata={"id": message.id, "created_at": message.created_at},
    )


def _last_assistant_message(session: AgentSession) -> LoopMessage | None:
    view = session.rebuild_view()
    for message in reversed(view.messages):
        if message.role == "assistant":
            return _agent_message_to_loop(message)
    return None


def _all_session_messages(session: AgentSession) -> tuple[LoopMessage, ...]:
    view = session.rebuild_view()
    return tuple(_agent_message_to_loop(message) for message in view.messages)


def _translate_tool_event(event: ToolExecutionEvent) -> AgentEvent | None:
    """把引擎的工具执行事件翻译成 pi 词汇的 tool_execution_* 事件。"""

    tool_call = event.tool_call
    if event.kind == "started":
        return ToolExecutionStartEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            args=tool_call.arguments,
        )
    if event.kind == "finished":
        return ToolExecutionEndEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            result=event.result,
            is_error=event.result is not None and not event.result.ok,
        )
    if event.kind in (
        "interrupted",
        "denied",
        "permission_requested",
        "prewrite_review",
        "background_started",
    ):
        return ToolExecutionUpdateEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            args=tool_call.arguments,
            partial_result=event.kind,
        )
    return None


async def agent_loop(
    prompts: list[LoopMessage],
    context: LoopContext,
    config: LoopConfig,
    signal: CancellationToken | None = None,
) -> AsyncIterator[AgentEvent]:
    """裸循环: 事件流外推,结束后以 ``AgentEndEvent.messages`` 携带全量会话消息。"""

    if not prompts:
        return

    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
    drive_error: list[BaseException] = []

    def on_stream(event: ChatStreamEvent) -> None:
        queue.put_nowait(
            MessageUpdateEvent(
                message=LoopMessage(role="assistant", content=event.text or ""),
                assistant_message_event=event,
            )
        )

    def on_tool(event: ToolExecutionEvent) -> None:
        translated = _translate_tool_event(event)
        if translated is not None:
            queue.put_nowait(translated)

    async def _drive() -> None:
        session_id = config.session_id or f"loop-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="lanscoder-loop-") as tmp:
            store = JsonlSessionStore(Path(tmp))
            session = AgentSession.create(
                store=store,
                session_id=session_id,
                tools=context.tools,
                permission_manager=None,
                sandbox_access=SandboxAccess(),
            )
            loop = create_agent_loop(
                session=session,
                provider=config.provider,
                limits=config.limits,
                request_options=config.request_options,
                context_window=config.context_window,
                background_manager=config.background_manager,
                guidance_provider=config.guidance_provider,
                cancellation_token=signal,
                stream_event_handler=on_stream,
                tool_event_handler=on_tool,
                enable_delegate_tool=False,
            )
            await queue.put(AgentStartEvent())
            for prompt in prompts:
                await queue.put(TurnStartEvent())
                await queue.put(MessageStartEvent(message=prompt))
                await loop.run_user_turn(prompt.content)
                assistant = _last_assistant_message(session)
                if assistant is not None:
                    await queue.put(MessageEndEvent(message=assistant))
                await queue.put(TurnEndEvent(message=assistant))
            await queue.put(AgentEndEvent(messages=_all_session_messages(session)))

    async def _wrap() -> None:
        try:
            await _drive()
        except BaseException as exc:  # noqa: BLE001 - 原样抛给消费端
            drive_error.append(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(_wrap())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        if not task.done():
            task.cancel()
    if drive_error:
        raise drive_error[0]
