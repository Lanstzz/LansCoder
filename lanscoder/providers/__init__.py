"""模型 provider 抽象和实现入口。"""

from lanscoder.providers.anthropic_provider import AnthropicProvider
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.factory import ProviderConfigError, create_provider_for_model
from lanscoder.providers.openai_compatible import OpenAICompatibleProvider
from lanscoder.providers.tool_adapters import to_anthropic_tool, to_openai_tool
from lanscoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderCapabilities,
    ProviderDiagnostics,
    StreamEventKind,
    TokenUsage,
    ToolChoiceFunction,
    ToolCall,
    ToolDefinition,
)

__all__ = [
    "AnthropicProvider",
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "ChatResponse",
    "ChatStreamEvent",
    "OpenAICompatibleProvider",
    "ProviderCapabilities",
    "ProviderConfigError",
    "ProviderDiagnostics",
    "StreamEventKind",
    "TokenUsage",
    "ToolChoiceFunction",
    "ToolCall",
    "ToolDefinition",
    "create_provider_for_model",
    "to_anthropic_tool",
    "to_openai_tool",
]
