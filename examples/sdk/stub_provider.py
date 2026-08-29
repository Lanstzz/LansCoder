"""SDK 示例共享 stub:无网络、可预置回复(含工具调用回合)的 ``ChatProvider``。

真实使用请换成 OpenAI / Anthropic 等 provider;示例只演示装配与事件流,
不访问任何外部服务。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse


@dataclass
class StubProvider(ChatProvider):
    """按顺序弹出预置回复的 provider;``tool_calls`` 非空时触发工具回合。"""

    replies: list[ChatResponse] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return "stub-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        return self.replies.pop(0)
