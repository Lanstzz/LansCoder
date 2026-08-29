"""P3 传输窄协议化:core 自有 ``LlmTransport`` Protocol,复用 providers 叶子类型。

``ChatProvider``(含默认 ``astream``)结构性满足;外部框架可提供 duck-typed
transport(2 方法 + 3 属性)驱动 L1/L3,无需继承 providers ABC。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderCapabilities,
)


@runtime_checkable
class LlmTransport(Protocol):
    """L1/L3 对 provider 的最小传输契约(D3)。

    仅复用 ``lanscoder.providers.types`` 叶子类型,无 app 依赖;字段名与
    ``ChatProvider`` 一致,自带 provider 零适配。
    """

    name: str
    model: str
    capabilities: ProviderCapabilities

    def complete(self, request: ChatRequest) -> ChatResponse: ...

    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]: ...
