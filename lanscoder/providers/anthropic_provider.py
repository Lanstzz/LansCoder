from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import (
    ProviderError,
    ProviderErrorKind,
    classify_provider_exception,
)
from lanscoder.providers.streaming import (
    STREAM_ENDED,
    StreamFailure,
    StreamToolCallAccumulator,
    complete_stream_tool_calls,
    merge_usage,
    read_field as _read_field,
    start_sync_stream_worker,
    token_usage,
)

if TYPE_CHECKING:
    from lanscoder.providers.types import TokenUsage
from lanscoder.providers.tool_adapters import to_anthropic_tool
from lanscoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    FinishReason,
    ProviderCapabilities,
    ProviderDiagnostics,
    ToolCall,
    ToolChoice,
    ToolChoiceFunction,
)
from lanscoder.utils.json_utils import loads_json_object


class AnthropicProvider(ChatProvider):

    def __init__(
        self,
        *,
        name: str = "anthropic",
        model: str,
        api_key: str,
        base_url: str | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        self._name = name
        self._model = model
        self._base_url = base_url
        self._capabilities = capabilities or ProviderCapabilities(
            supports_streaming=True,
            supports_forced_tool_choice=True,
            supports_parallel_tool_calls=True,
        )
        self._extra_headers = dict(extra_headers or {})
        self._extra_body = dict(extra_body or {})

        if client is not None:
            self._client = client
        else:
            from anthropic import Anthropic

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            if extra_headers:
                kwargs["default_headers"] = extra_headers
            self._client = Anthropic(**kwargs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def extra_headers(self) -> dict[str, str]:
        return dict(self._extra_headers)

    @property
    def extra_body(self) -> dict[str, Any]:
        return dict(self._extra_body)

    def complete(self, request: ChatRequest) -> ChatResponse:

        params = self._build_message_params(request)
        try:
            response = self._client.messages.create(**params)
        except Exception as exc:
            raise ProviderError(classify_provider_exception(exc), str(exc)) from exc

        content_blocks = _read_field(response, "content", []) or []
        raw_finish_reason = _read_field(response, "stop_reason")
        finish_reason = _normalize_stop_reason(raw_finish_reason)
        diagnostics = ProviderDiagnostics(raw_finish_reason=raw_finish_reason)
        diagnostics.reasoning = _collect_thinking(content_blocks) or None
        tool_calls = self._parse_tool_calls(content_blocks, diagnostics=diagnostics)
        if finish_reason == "length" and tool_calls:
            diagnostics.warnings.append("finish_reason=length，丢弃可能不完整的 tool_calls，避免执行半截工具调用。")
            tool_calls = []

        return ChatResponse(
            provider=self.name,
            model=_read_field(response, "model", self._model),
            content=self._collect_text(content_blocks),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_parse_usage(_read_field(response, "usage")),
            diagnostics=diagnostics,
            raw=response,
        )

    async def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:

        if not self._capabilities.supports_streaming:
            raise ProviderError(
                ProviderErrorKind.UNSUPPORTED,
                f"provider {self.name} 不支持 streaming",
            )

        params = self._build_message_params(request)
        params["stream"] = True
        diagnostics = ProviderDiagnostics()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_accumulators: dict[int, StreamToolCallAccumulator] = {}
        raw_finish_reason: Any = None
        response_model = self._model
        usage: TokenUsage | None = None

        try:
            stream = await asyncio.to_thread(self._client.messages.create, **params)
        except Exception as exc:
            raise ProviderError(classify_provider_exception(exc), str(exc)) from exc

        yield ChatStreamEvent(kind="message_started")

        stream_queue, stop_stream = start_sync_stream_worker(
            stream,
            thread_name="lanscoder-anthropic-stream",
        )
        try:
            while True:
                item = await asyncio.to_thread(stream_queue.get)
                if item is STREAM_ENDED:
                    break
                if isinstance(item, StreamFailure):
                    raise ProviderError(
                        classify_provider_exception(item.error),
                        str(item.error),
                    ) from item.error

                event = item
                event_type = _read_field(event, "type")

                if event_type == "message_start":
                    message = _read_field(event, "message")
                    if message is not None:
                        response_model = _read_field(message, "model", response_model) or response_model
                        usage = merge_usage(usage, _parse_usage(_read_field(message, "usage")))
                    continue

                if event_type == "content_block_start":
                    index = int(_read_field(event, "index", 0) or 0)
                    block = _read_field(event, "content_block", {}) or {}
                    block_type = _read_field(block, "type")
                    if block_type == "tool_use":
                        accumulator = StreamToolCallAccumulator(
                            index=index,
                            id=str(_read_field(block, "id", "") or ""),
                            name=str(_read_field(block, "name", "") or ""),
                        )
                        initial_input = _read_field(block, "input")
                        if isinstance(initial_input, dict) and initial_input:
                            from lanscoder.utils.json_utils import dumps_json

                            accumulator.arguments_text = dumps_json(initial_input)
                            accumulator.saw_arguments = True
                        tool_accumulators[index] = accumulator
                        yield ChatStreamEvent(
                            kind="tool_call_started",
                            tool_call_index=index,
                            tool_call_id=accumulator.id,
                            tool_name=accumulator.name,
                        )
                    continue

                if event_type == "content_block_delta":
                    index = int(_read_field(event, "index", 0) or 0)
                    delta = _read_field(event, "delta", {}) or {}
                    delta_type = _read_field(delta, "type")

                    if delta_type == "text_delta":
                        text = _read_field(delta, "text") or ""
                        if text:
                            content_parts.append(text)
                            yield ChatStreamEvent(kind="text_delta", text=text)
                        continue

                    if delta_type in {"thinking_delta", "reasoning_delta"}:
                        text = _read_field(delta, "thinking") or _read_field(delta, "text") or ""
                        if text:
                            reasoning_parts.append(text)
                            yield ChatStreamEvent(kind="reasoning_delta", text=text)
                        continue

                    if delta_type == "input_json_delta":
                        partial = _read_field(delta, "partial_json") or ""
                        accumulator = tool_accumulators.get(index)
                        if accumulator is None:
                            accumulator = StreamToolCallAccumulator(index=index)
                            tool_accumulators[index] = accumulator
                            yield ChatStreamEvent(
                                kind="tool_call_started",
                                tool_call_index=index,
                                tool_call_id=accumulator.id,
                                tool_name=accumulator.name,
                            )
                        if partial:
                            accumulator.arguments_text += partial
                            accumulator.saw_arguments = True
                            yield ChatStreamEvent(
                                kind="tool_call_delta",
                                tool_call_index=index,
                                tool_call_id=accumulator.id,
                                tool_name=accumulator.name,
                                arguments_delta=partial,
                            )
                        continue
                    continue

                if event_type == "message_delta":
                    delta = _read_field(event, "delta", {}) or {}
                    stop_reason = _read_field(delta, "stop_reason")
                    if stop_reason is not None:
                        raw_finish_reason = stop_reason
                    usage = merge_usage(usage, _parse_usage(_read_field(event, "usage")))
                    continue

                if event_type == "error":
                    error = _read_field(event, "error") or event
                    message = _read_field(error, "message") or str(error)
                    diagnostics.warnings.append(message)
                    yield ChatStreamEvent(kind="error", diagnostics=diagnostics)
                    raise ProviderError(classify_provider_exception(Exception(message)), message)
        finally:
            await asyncio.to_thread(stop_stream)

        finish_reason = _normalize_stop_reason(raw_finish_reason)
        diagnostics.raw_finish_reason = raw_finish_reason
        if reasoning_parts:
            diagnostics.reasoning = "".join(reasoning_parts)

        tool_calls: list[ToolCall] = []
        if tool_accumulators and finish_reason != "tool_calls":
            diagnostics.warnings.append(f"finish_reason={finish_reason}，丢弃 streaming 中未以 tool_calls 完成的 tool_calls。")
        elif tool_accumulators:
            tool_calls = complete_stream_tool_calls(
                tool_accumulators,
                diagnostics,
                require_identity=False,
            )

        for tool_call in tool_calls:
            yield ChatStreamEvent(
                kind="tool_call_completed",
                tool_call=tool_call,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
            )

        response = ChatResponse(
            provider=self.name,
            model=response_model,
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            diagnostics=diagnostics,
            raw=None,
        )
        yield ChatStreamEvent(kind="message_completed", response=response, diagnostics=diagnostics)

    def _build_message_params(self, request: ChatRequest) -> dict[str, Any]:
        if request.tools and not self._capabilities.supports_tools:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"provider {self.name} 不支持 tool calling，不能发送 tools",
            )

        params: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_anthropic_messages(request.messages),
            "max_tokens": request.max_tokens or 4096,
        }

        system_prompt = self._collect_system_prompt(request.messages)
        if system_prompt:
            params["system"] = system_prompt

        if request.tools:
            params["tools"] = [to_anthropic_tool(tool) for tool in request.tools]
            params["tool_choice"] = self._to_anthropic_tool_choice(request.tool_choice)

        if request.temperature is not None:
            params["temperature"] = request.temperature

        extra_body = {**self._extra_body, **request.extra_body}
        if extra_body:
            reserved = {
                "model",
                "messages",
                "max_tokens",
                "tools",
                "tool_choice",
                "system",
                "stream",
                "temperature",
            }
            for key, value in extra_body.items():
                if key in reserved:
                    continue
                params[key] = value
        return params

    def _to_anthropic_tool_choice(self, tool_choice: ToolChoice | None) -> dict[str, Any]:

        if tool_choice is None or tool_choice == "auto":
            payload: dict[str, Any] = {"type": "auto"}
        elif tool_choice == "none":
            payload = {"type": "none"}
        elif tool_choice == "required":
            payload = {"type": "any"}
        elif isinstance(tool_choice, ToolChoiceFunction):
            if not self._capabilities.supports_forced_tool_choice:
                raise ProviderError(
                    ProviderErrorKind.CONFIG_ERROR,
                    "当前 AnthropicProvider 未启用 forced tool_choice",
                )
            payload = {"type": "tool", "name": tool_choice.name}
        else:
            raise ProviderError(ProviderErrorKind.CONFIG_ERROR, f"不支持的 tool_choice：{tool_choice!r}")

        if not self._capabilities.supports_parallel_tool_calls:
            payload["disable_parallel_tool_use"] = True
        return payload

    @staticmethod
    def _collect_system_prompt(messages: list[ChatMessage]) -> str:

        return "\n\n".join(message.content for message in messages if message.role == "system")

    @staticmethod
    def _to_anthropic_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:

        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
                if (
                    converted
                    and converted[-1]["role"] == "user"
                    and isinstance(converted[-1]["content"], list)
                    and all(isinstance(item, dict) and item.get("type") == "tool_result" for item in converted[-1]["content"])
                ):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
                continue
            if message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for tool_call in message.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "input": _tool_call_input(tool_call),
                        }
                    )
                converted.append({"role": "assistant", "content": content})
                continue
            if message.content_parts is not None:
                content = []
                for part in message.content_parts:
                    if part.type == "text" and part.text is not None:
                        content.append({"type": "text", "text": part.text})
                    elif part.type == "image" and part.media_type and part.data_base64:
                        content.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": part.media_type,
                                    "data": part.data_base64,
                                },
                            }
                        )
                converted.append({"role": message.role, "content": content or message.content})
            else:
                converted.append({"role": message.role, "content": message.content})
        return converted

    @staticmethod
    def _collect_text(content_blocks: list[Any]) -> str:

        parts: list[str] = []
        for block in content_blocks:
            if _read_field(block, "type") == "text":
                parts.append(_read_field(block, "text", "") or "")
        return "".join(parts)

    @staticmethod
    def _parse_tool_calls(
        content_blocks: list[Any],
        *,
        diagnostics: ProviderDiagnostics,
    ) -> list[ToolCall]:

        parsed: list[ToolCall] = []
        for block in content_blocks:
            if _read_field(block, "type") != "tool_use":
                continue
            raw_input = _read_field(block, "input", {}) or {}
            if isinstance(raw_input, str):
                arguments = loads_json_object(raw_input)
                if not isinstance(arguments, dict):
                    call_id = _read_field(block, "id", "")
                    name = _read_field(block, "name", "")
                    diagnostics.warnings.append(f"tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用：id={call_id}, name={name}")
                    return []
            elif isinstance(raw_input, dict):
                arguments = raw_input
            else:
                call_id = _read_field(block, "id", "")
                name = _read_field(block, "name", "")
                diagnostics.warnings.append(f"tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用：id={call_id}, name={name}")
                return []

            parsed.append(
                ToolCall(
                    id=_read_field(block, "id", ""),
                    name=_read_field(block, "name", ""),
                    arguments=arguments,
                )
            )
        return parsed


def _tool_call_input(tool_call: ToolCall) -> dict[str, Any]:
    if isinstance(tool_call.arguments, dict):
        return tool_call.arguments
    if isinstance(tool_call.arguments, str):
        parsed = loads_json_object(tool_call.arguments)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _collect_thinking(content_blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in content_blocks:
        block_type = _read_field(block, "type")
        if block_type in {"thinking", "reasoning"}:
            text = _read_field(block, "thinking") or _read_field(block, "text") or ""
            if text:
                parts.append(text)
    return "".join(parts)


def _normalize_stop_reason(reason: Any) -> FinishReason:

    if reason == "end_turn" or reason == "stop_sequence":
        return "stop"
    if reason == "tool_use":
        return "tool_calls"
    if reason == "max_tokens":
        return "length"
    if reason is None:
        return "unknown"
    return "unknown"


def _parse_usage(usage: Any):

    if usage is None:
        return None
    input_tokens = _read_field(usage, "input_tokens")
    output_tokens = _read_field(usage, "output_tokens")
    return token_usage(input_tokens, output_tokens, _read_field(usage, "total_tokens"))
