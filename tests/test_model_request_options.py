from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from lanscoder.agent.session import AgentSession
from lanscoder.app.runtime import AgentChatRunner, CurrentSessionState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    MainRequestOptions,
)
from lanscoder.utils.cancellation import CancellationToken


@dataclass
class RecordingProvider(ChatProvider):
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "recording"

    @property
    def model(self) -> str:
        return "recording-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(provider=self.name, model=self.model, content="ok")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        yield ChatStreamEvent(kind="message_completed", response=ChatResponse(provider=self.name, model=self.model, content="ok"))


def _session(tmp_path) -> AgentSession:
    return AgentSession.create(store=JsonlSessionStore(tmp_path), session_id="sess_options", agents_md="")


def test_main_sync_request_inherits_selected_model_options(tmp_path, make_loop) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = make_loop(
        session=session,
        provider=provider,
        request_options=MainRequestOptions(
            temperature=0.2,
            max_tokens=8192,
            extra_body={"reasoning_effort": "high"},
        ),
    )
    session.append_user_message("检查 README")

    asyncio.run(loop._complete_once(streaming=False))

    request = provider.requests[-1]
    assert request.temperature == 0.2
    assert request.max_tokens == 8192
    assert request.extra_body == {"reasoning_effort": "high"}


def test_main_stream_request_inherits_selected_model_options(tmp_path, make_loop) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = make_loop(
        session=session,
        provider=provider,
        request_options=MainRequestOptions(
            temperature=0.3,
            max_tokens=4096,
            extra_body={"reasoning_effort": "medium"},
        ),
    )
    session.append_user_message("检查 README")

    asyncio.run(loop._complete_once(streaming=True))

    request = provider.requests[-1]
    assert request.temperature == 0.3
    assert request.max_tokens == 4096
    assert request.extra_body == {"reasoning_effort": "medium"}


def test_main_request_options_copy_extra_body() -> None:
    options_body = {"nested": {"value": 1}}
    options = MainRequestOptions(extra_body=options_body)
    options_body["nested"]["value"] = 2
    kwargs = options.as_chat_request_kwargs()
    kwargs["extra_body"]["nested"]["value"] = 3

    assert options.extra_body == {"nested": {"value": 1}}


def test_chat_runner_passes_context_window_to_agent_loop(tmp_path) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=provider,
        context_window=128_000,
        request_options=MainRequestOptions(max_tokens=8_192),
    )

    loop = runner._create_loop(CancellationToken())

    assert loop.context_window == 128_000
    assert loop.request_options.max_tokens == 8_192


class RetryableOnceProvider(ChatProvider):
    """complete 第一次抛 retryable 错误，之后成功；astream 委托给 base。"""

    def __init__(self, base: RecordingProvider) -> None:
        self._base = base
        self._failures = 0

    @property
    def name(self) -> str:
        return self._base.name

    @property
    def model(self) -> str:
        return self._base.model

    def complete(self, request: ChatRequest) -> ChatResponse:
        self._base.requests.append(request)
        if self._failures == 0:
            self._failures += 1
            raise ProviderError(ProviderErrorKind.SERVER_ERROR, "boom")
        return ChatResponse(provider=self.name, model=self.model, content="ok")

    async def astream(self, request: ChatRequest):
        async for event in self._base.astream(request):
            yield event


def test_unified_complete_once_sync_mode_returns_provider_response(tmp_path, make_loop) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = make_loop(session=session, provider=provider)
    session.append_user_message("hi")
    response = asyncio.run(loop._complete_once(streaming=False))
    assert response.content == "ok"


def test_unified_complete_once_streaming_mode_collects_events(tmp_path, make_loop) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = make_loop(session=session, provider=provider)
    session.append_user_message("hi")
    response = asyncio.run(loop._complete_once(streaming=True))
    assert response.content == "ok"
    assert [e.kind for e in loop.last_stream_events] == ["message_completed"]


class IncompleteStreamProvider(RecordingProvider):
    """astream 只产出局部 delta 就结束，不发送 message_completed。"""

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        yield ChatStreamEvent(kind="text_delta", text="partial")


def test_unified_complete_once_stream_without_completed_rolls_back_events(tmp_path, make_loop) -> None:
    session = _session(tmp_path)
    loop = make_loop(session=session, provider=IncompleteStreamProvider())
    session.append_user_message("hi")
    with pytest.raises(ProviderError):
        asyncio.run(loop._complete_once(streaming=True))
    assert loop.last_stream_events == []


def test_unified_recovery_retries_retryable_error_once_for_sync_mode(tmp_path, make_loop) -> None:
    # 关键新行为（spec 4.3）：非流式也获得 retryable 重试
    base = RecordingProvider()
    session = _session(tmp_path)
    loop = make_loop(session=session, provider=RetryableOnceProvider(base))
    session.append_user_message("hi")
    response = asyncio.run(loop._complete_once_with_recovery(streaming=False))
    assert response.content == "ok"
    assert loop.guardrails.call_count == 2
