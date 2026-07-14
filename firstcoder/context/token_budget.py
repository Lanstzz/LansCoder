"""上下文 token 预算的集中估算。"""

from __future__ import annotations

from dataclasses import dataclass
import json

from firstcoder.providers.types import ChatMessage, ToolDefinition

from firstcoder.context.models import AgentMessage, MessagePart


def estimate_text_tokens(text: str) -> int:
    """第一版使用字符数近似 token。

    这里有意不绑定具体 tokenizer，避免 context 层过早依赖 provider。后续可以按 provider
    能力替换实现，但调用点仍然走 `TokenBudgetService`。
    """

    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_chat_request_tokens(
    *,
    messages: list[ChatMessage],
    tools: list[ToolDefinition],
    reserved_output_tokens: int = 0,
) -> int:
    """Estimate the model-facing request, including schema and output reserve.

    This remains provider-neutral and deliberately uses the same cheap text
    heuristic as the rest of the context layer.  Unlike a tail-only estimate,
    it includes all request material that occupies the provider window.
    """

    message_tokens = sum(
        estimate_text_tokens(message.content)
        + sum(estimate_text_tokens(call.name + json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)) for call in message.tool_calls)
        for message in messages
    )
    tool_tokens = sum(
        estimate_text_tokens(
            json.dumps(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for tool in tools
    )
    return message_tokens + tool_tokens + max(0, reserved_output_tokens)


@dataclass(slots=True)
class TokenBudget:
    context_window: int
    reserved_output_tokens: int
    effective_window: int
    warning_threshold: int
    auto_compact_threshold: int
    blocking_threshold: int


@dataclass(slots=True)
class TokenBudgetService:
    context_window: int
    provider_max_output_tokens: int

    def build_budget(self) -> TokenBudget:
        reserved = min(self.provider_max_output_tokens, 16_000)
        effective = max(0, self.context_window - reserved)
        return TokenBudget(
            context_window=self.context_window,
            reserved_output_tokens=reserved,
            effective_window=effective,
            warning_threshold=effective * 70 // 100,
            auto_compact_threshold=effective * 82 // 100,
            blocking_threshold=effective * 95 // 100,
        )

    def estimate_part_tokens(self, part: MessagePart) -> int:
        return estimate_text_tokens(part.content)

    def estimate_message_tokens(self, message: AgentMessage) -> int:
        return sum(self.estimate_part_tokens(part) for part in message.parts)
