from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent


class ChatProvider(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def model(self) -> str:
        pass

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        pass

    async def acomplete(self, request: ChatRequest) -> ChatResponse:

        import asyncio

        return await asyncio.to_thread(self.complete, request)

    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:

        async def unsupported_stream() -> AsyncIterator[ChatStreamEvent]:
            for event in ():
                yield event
            raise ProviderError(
                ProviderErrorKind.UNSUPPORTED,
                f"provider {self.name} 还没有实现 streaming",
            )

        return unsupported_stream()
