from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

MessageRole = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal[
    "stop",
    "tool_calls",
    "length",
    "content_filter",
    "error",
    "unknown",
    "tool_round_limit",
    "waiting_for_user_input",
]
TokenParam = Literal["max_tokens", "max_completion_tokens"]
ToolChoiceMode = Literal["auto", "none", "required"]
StreamEventKind = Literal[
    "message_started",
    "reasoning_delta",
    "text_delta",
    "tool_call_started",
    "tool_call_delta",
    "tool_call_completed",
    "message_completed",
    "error",
]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:

    supports_tools: bool = True
    supports_forced_tool_choice: bool = True
    supports_streaming: bool = False
    supports_parallel_tool_calls: bool = False
    supports_json_mode: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    token_param: TokenParam = "max_tokens"


@dataclass(slots=True)
class TokenUsage:

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class ProviderDiagnostics:

    reasoning: str | None = None
    # 流式：首个 reasoning_delta 到首个 text_delta/tool_call_started/message_completed 的墙钟间隔；
    # 非流式：整轮 complete 调用耗时（近似），仅在有 reasoning 时写入。
    reasoning_seconds: float | None = None
    raw_finish_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContentPart:

    type: Literal["text", "image"]
    text: str | None = None
    media_type: str | None = None
    data_base64: str | None = None
    filename: str | None = None


@dataclass(slots=True)
class ChatMessage:

    role: MessageRole
    content: str
    content_parts: list[ContentPart] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(slots=True)
class ToolDefinition:

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCall:

    id: str
    name: str
    arguments: dict[str, Any] | str


@dataclass(frozen=True, slots=True)
class ToolChoiceFunction:

    name: str


ToolChoice = ToolChoiceMode | ToolChoiceFunction


@dataclass(frozen=True, slots=True)
class MainRequestOptions:

    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra_body", deepcopy(dict(self.extra_body)))

    def as_chat_request_kwargs(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_body": deepcopy(self.extra_body),
        }


@dataclass(slots=True)
class ChatRequest:

    messages: list[ChatMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
    tool_choice: ToolChoice | None = "auto"
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatResponse:

    provider: str
    model: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None
    diagnostics: ProviderDiagnostics = field(default_factory=ProviderDiagnostics)
    raw: Any | None = None


@dataclass(slots=True)
class ChatStreamEvent:

    kind: StreamEventKind
    text: str = ""
    tool_call: ToolCall | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""
    response: ChatResponse | None = None
    diagnostics: ProviderDiagnostics = field(default_factory=ProviderDiagnostics)
