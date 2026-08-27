"""L2 Agent:有状态 wrapper,内部驱动 L1,照搬 pi 的 subscribe / prompt / steer / follow_up / abort。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from lanscoder.utils.cancellation import CancellationToken

from lanscoder.core.agent_loop import agent_loop
from lanscoder.core.events import AgentEndEvent, AgentEvent
from lanscoder.core.messages import LoopContext, LoopConfig, LoopMessage

Listener = Callable[[AgentEvent], Awaitable[None] | None]


def _normalize_prompts(input_: str | LoopMessage | list[LoopMessage]) -> list[LoopMessage]:
    if isinstance(input_, list):
        return input_
    if isinstance(input_, LoopMessage):
        return [input_]
    return [LoopMessage.user(input_)]


class Agent:
    """有状态 wrapper: 持有 LoopContext / LoopConfig,内部驱动 L1 的 agent_loop。"""

    def __init__(self, context: LoopContext, config: LoopConfig) -> None:
        self._context = context
        self._config = config
        self._listeners: list[Listener] = []
        self._steering: list[LoopMessage] = []
        self._follow_ups: list[LoopMessage] = []
        self._cancellation = CancellationToken()
        self._active_task: asyncio.Task[None] | None = None
        self._messages: list[LoopMessage] = list(context.messages)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def _dispatch(self, event: AgentEvent) -> None:
        for listener in self._listeners:
            result = listener(event)
            if result is not None:
                await result

    def steer(self, message: LoopMessage) -> None:
        """排队一条消息,在当前回合(assistant turn)结束后注入。"""

        self._steering.append(message)

    def follow_up(self, message: LoopMessage) -> None:
        """排队一条消息,仅在 agent 本要停止时注入。"""

        self._follow_ups.append(message)

    def abort(self) -> None:
        """中断当前回合与排队消息。"""

        self._cancellation.cancel()
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()

    async def _run_loop(self, prompts: list[LoopMessage]) -> None:
        async for event in agent_loop(prompts, self._context, self._config, self._cancellation):
            if isinstance(event, AgentEndEvent):
                self._messages = list(event.messages)
            await self._dispatch(event)
        self._context.messages = self._messages

    async def prompt(self, input_: str | LoopMessage | list[LoopMessage]) -> None:
        """驱动一轮(或多轮)回合;完成后执行 steer / follow_up 队列。"""

        if self._active_task is not None:
            raise RuntimeError("agent is already processing a prompt; use steer() or follow_up()")

        prompts = _normalize_prompts(input_)

        async def _run() -> None:
            await self._run_loop(prompts)
            while self._steering:
                await self._run_loop([self._steering.pop(0)])
            while self._follow_ups:
                await self._run_loop([self._follow_ups.pop(0)])

        self._active_task = asyncio.create_task(_run())
        try:
            await self._active_task
        finally:
            self._active_task = None
