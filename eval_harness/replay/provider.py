"""A no-I/O provider that replays an explicit response tape."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities, ToolCall

from eval_harness.schema.models import ProviderTapeResponse


@dataclass(slots=True)
class ScriptedProvider(ChatProvider):
    """Deterministically serve tape entries and expose every interaction to a recorder."""

    tape: tuple[ProviderTapeResponse, ...]
    on_interaction: Callable[[str, dict[str, Any]], None] | None = None
    _position: int = field(default=0, init=False)
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return "offline-tape-v1"

    @property
    def calls(self) -> int:
        return self._position

    def complete(self, request: ChatRequest) -> ChatResponse:
        self._emit("provider_request", _request_payload(request))
        if self._position >= len(self.tape):
            raise RuntimeError("scripted provider tape exhausted")
        entry = self.tape[self._position]
        self._position += 1
        if entry.fault is not None:
            kind = _fault_kind(entry.fault)
            self._emit(
                "provider_error",
                {
                    "kind": entry.fault,
                    "provider_error_kind": kind.value,
                    "message": f"scripted {entry.fault} injected",
                    "recoverable": kind in {ProviderErrorKind.TIMEOUT, ProviderErrorKind.NETWORK_ERROR},
                },
            )
            raise ProviderError(kind, f"scripted {entry.fault} injected")
        response = ChatResponse(
            provider=self.name,
            model=self.model,
            content=entry.content,
            tool_calls=[
                ToolCall(
                    id=str(call["id"]),
                    name=str(call["name"]),
                    arguments=call.get("arguments", {}),
                )
                for call in entry.tool_calls
            ],
            finish_reason=entry.finish_reason,
        )
        self._emit(
            "provider_response",
            {
                "provider": response.provider,
                "model": response.model,
                "content": response.content,
                "finish_reason": response.finish_reason,
                "tool_calls": [_tool_call_payload(call) for call in response.tool_calls],
            },
        )
        return response

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.on_interaction is not None:
            self.on_interaction(event_type, data)


def _request_payload(request: ChatRequest) -> dict[str, Any]:
    return {
        "messages": [_request_message_payload(message) for message in request.messages],
        "tools": [
            {
                "name": tool.name,
                "description": "<TOOL_DESCRIPTION_REDACTED>",
                "description_sha256": _fingerprint(tool.description),
                "parameters": "<TOOL_PARAMETERS_REDACTED>",
                "parameters_sha256": _fingerprint(tool.parameters),
                "parameter_keys": sorted(tool.parameters) if isinstance(tool.parameters, dict) else [],
            }
            for tool in request.tools
        ],
        "tool_choice": str(request.tool_choice),
    }


def _request_message_payload(message: Any) -> dict[str, Any]:
    content = message.content
    payload: dict[str, Any] = {
        "role": message.role,
        "content": content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [_tool_call_payload(call) for call in message.tool_calls],
    }
    if message.role == "system":
        payload["content"] = "<SYSTEM_PROMPT_REDACTED>"
        payload["content_sha256"] = _system_prompt_fingerprint(content)
    elif message.role == "tool":
        payload["content"] = "<TOOL_RESULT_REDACTED>"
        payload["content_sha256"] = _fingerprint(content)
    return payload


def _system_prompt_fingerprint(content: str) -> str:
    """Track prompt changes without persisting the prompt itself in a portable trace."""

    normalized = re.sub(r"\b(?:msg|part)_[0-9a-f]{8,}\b", "<RUNTIME_ID>", content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tool_call_payload(call: Any) -> dict[str, Any]:
    arguments = call.arguments
    return {
        "id": call.id,
        "name": call.name,
        "arguments_sha256": _fingerprint(arguments),
        "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
    }


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fault_kind(fault: str) -> ProviderErrorKind:
    """Map portable fault labels to the runtime's existing error taxonomy."""

    return {
        "malformed_response": ProviderErrorKind.API_ERROR,
        "timeout": ProviderErrorKind.TIMEOUT,
        "prompt_too_long": ProviderErrorKind.PROMPT_TOO_LONG,
        "network_error": ProviderErrorKind.NETWORK_ERROR,
    }[fault]
