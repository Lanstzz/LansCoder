"""Loop 协作对象契约测试：签名固定 + 纯变换行为（零 loop 状态）。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import pytest

from lanscoder.agent.guardrails import TurnGuardrails
from lanscoder.agent.loop_limits import _AgentLoopLimitReached, AgentLoopLimits
from lanscoder.agent.request_builder import PreparedMainRequest, RequestBuilder
from lanscoder.agent.session import AgentSession
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import (
    ChatRequest,
    ChatResponse,
    MainRequestOptions,
    ProviderCapabilities,
    ToolDefinition,
)


@dataclass
class FakeProvider(ChatProvider):
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("contract test should not call provider")


def test_request_builder_build_contract_signature() -> None:
    params = list(inspect.signature(RequestBuilder.build).parameters)
    assert params[0] == "self" and params[1] == "view" and "definitions" in params
    assert inspect.signature(RequestBuilder.build).parameters["definitions"].kind == inspect.Parameter.KEYWORD_ONLY
    bparams = list(inspect.signature(RequestBuilder.context_budget_for_view).parameters)
    assert bparams[1] == "view" and "runtime_instruction" in bparams and "definitions" in bparams


def test_request_builder_build_pure_behavior(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_contract", agents_md="")
    session.append_user_message("hello")
    context_builder = ContextBuilder()
    builder = RequestBuilder(
        session=session,
        provider=FakeProvider(),
        context_builder=context_builder,
        request_options=MainRequestOptions(),
        context_window=32768,
    )
    view = session.rebuild_view()
    d1 = ToolDefinition(name="echo", description="回显文本", parameters={})

    prepared = builder.build(view, definitions=[d1])

    assert isinstance(prepared, PreparedMainRequest)
    assert prepared.request.messages[0].role == "system"
    assert any(message.role == "user" and "hello" in message.content for message in prepared.request.messages)
    assert prepared.request.tools == [d1]
    assert prepared.request.tool_choice == "auto"
    assert prepared.tool_result_part_ids == context_builder.projected_tool_result_part_ids(view)
    assert prepared.request_id
    assert prepared.projection_fingerprint


def test_turn_guardrails_contract_signature() -> None:
    for name in ("reserve_call", "check_timeout", "begin_turn"):
        assert list(inspect.signature(getattr(TurnGuardrails, name)).parameters) == ["self"]
    assert "reason" in inspect.signature(TurnGuardrails.limit_response).parameters
    guardrails = TurnGuardrails(provider=FakeProvider(), limits=AgentLoopLimits.default())
    assert isinstance(guardrails.call_count, int)


def test_turn_guardrails_behavior() -> None:
    clock_now = {"value": 100.0}

    def clock() -> float:
        return clock_now["value"]

    guardrails = TurnGuardrails(
        provider=FakeProvider(),
        limits=AgentLoopLimits(max_provider_calls=1, max_turn_seconds=5),
        clock=clock,
    )
    guardrails.begin_turn()
    guardrails.reserve_call()
    assert guardrails.call_count == 1
    with pytest.raises(_AgentLoopLimitReached):
        guardrails.reserve_call()
    guardrails.begin_turn()
    assert guardrails.call_count == 0
    clock_now["value"] = 110.0
    with pytest.raises(_AgentLoopLimitReached):
        guardrails.check_timeout()
