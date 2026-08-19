from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lanscoder.context.llm_compact import (
    CODING_HANDOFF_HEADINGS,
    DIALOGUE_SUMMARY_HEADINGS,
    LlmCompactSummary,
    NoSummaryError,
    PromptTooLongError,
    normalize_coding_handoff,
)
from lanscoder.context.models import AgentMessage, MessagePart
from lanscoder.context.provider_summarizer import (
    ProviderLlmCompactSummarizer,
    _build_dialogue_summary_prompt,
    _message_text,
    _tail_boundary,
)
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatRequest, ChatResponse

EXPECTED_DIALOGUE_SUMMARY_HEADINGS = (
    "## 用户请求要点",
    "## 已给出的结论",
    "## 未完成事项",
    "## 关键约束与偏好",
)


@dataclass
class FakeProvider(ChatProvider):
    response: ChatResponse | ProviderError
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if isinstance(self.response, ProviderError):
            raise self.response
        return self.response


def test_provider_summarizer_requests_plain_summary_without_tools() -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="摘要"))

    summary = ProviderLlmCompactSummarizer(provider).summarize(
        [
            _message("msg_1", "user", "目标", created_turn=1),
            _message("msg_2", "assistant", "进展", created_turn=2),
        ],
        current_turn=2,
        recent_turn_window=1,
    )

    assert isinstance(summary, LlmCompactSummary)
    assert "## 用户请求要点\n摘要" in summary.summary
    assert summary.summary.count("## ") == 4
    assert summary.covered_until_message_id == "msg_1"
    assert summary.tail_start_message_id == "msg_2"
    assert provider.requests[0].tools == []
    assert provider.requests[0].tool_choice == "none"


def test_provider_summarizer_keeps_tool_call_sequence_in_tail() -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="摘要"))

    summary = ProviderLlmCompactSummarizer(provider).summarize(
        [
            _message("msg_1", "user", "目标", created_turn=1),
            _assistant_tool_call("msg_2", "call_1", created_turn=2),
            _tool_result("msg_3", "call_1", created_turn=3),
        ],
        current_turn=3,
        recent_turn_window=1,
    )

    assert summary.covered_until_message_id == "msg_1"
    assert summary.tail_start_message_id == "msg_2"


def test_provider_summarizer_maps_prompt_too_long_provider_error() -> None:
    provider = FakeProvider(ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long"))

    with pytest.raises(PromptTooLongError):
        ProviderLlmCompactSummarizer(provider).summarize(
            [
                _message("msg_1", "user", "目标", created_turn=1),
                _message("msg_2", "assistant", "进展", created_turn=2),
            ],
            current_turn=2,
            recent_turn_window=1,
        )


def test_provider_summarizer_rejects_too_short_history() -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="摘要"))

    with pytest.raises(NoSummaryError):
        ProviderLlmCompactSummarizer(provider).summarize([_message("msg_1", "user", "目标")])


def test_provider_summarizer_prompt_and_normalizer_enforce_exact_dialogue_headings() -> None:
    assert DIALOGUE_SUMMARY_HEADINGS == EXPECTED_DIALOGUE_SUMMARY_HEADINGS
    model_output = "\n".join(
        [
            "## 用户请求要点",
            "Implement L1.",
            "## 用户请求要点",
            "Keep the latest user message.",
            "## 未完成事项",
            "A failing test remains.",
            "## Extra model heading",
            "Keep this as body text.",
        ]
    )
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content=model_output))

    summary = ProviderLlmCompactSummarizer(provider).summarize(
        [
            _message("msg_1", "user", "目标", created_turn=1),
            _message("msg_2", "assistant", "进展", created_turn=2),
        ],
        current_turn=2,
        recent_turn_window=1,
    )

    assert all(summary.summary.count(heading) == 1 for heading in DIALOGUE_SUMMARY_HEADINGS)
    assert "Implement L1.\nKeep the latest user message." in summary.summary
    assert "A failing test remains.\nExtra model heading\nKeep this as body text." in summary.summary
    assert "## 已给出的结论\n无" in summary.summary
    prompt = provider.requests[0].messages[1].content
    assert all(prompt.count(heading) == 1 for heading in DIALOGUE_SUMMARY_HEADINGS)
    assert "只输出一次" in prompt


def test_normalize_coding_handoff_preserves_body_under_matching_heading() -> None:
    normalized = normalize_coding_handoff("## 下一步（可立即执行）\nRun focused tests.")

    assert normalized.endswith("## 下一步（可立即执行）\nRun focused tests.")
    assert all(normalized.count(heading) == 1 for heading in CODING_HANDOFF_HEADINGS)


def test_dialogue_prompt_emits_message_text_but_omits_tool_call_argument_dumps() -> None:
    tool_call = _assistant_tool_call("msg_2", "call_1", created_turn=2)
    tool_call.parts[0].metadata["arguments"] = {"command": "pytest -q", "secret_value": "s3cr3t"}
    tool_call.parts[0].content = "tool call body"
    messages = [
        _message("msg_1", "user", "目标", created_turn=1),
        tool_call,
    ]

    prompt = _build_dialogue_summary_prompt(messages, summary_mode="default")

    assert "[msg_1] role=user\n目标" in prompt
    assert "[msg_2] role=assistant\ntool call body" in prompt
    assert "s3cr3t" not in prompt
    assert '"command"' not in prompt
    assert all(_message_text(message) in prompt for message in messages)


def test_tail_boundary_skips_messages_without_created_turn() -> None:
    messages = [
        _message("msg_0", "user", "旧消息"),
        _user_message_with_turn("msg_1", created_turn=1),
        _user_message_with_turn("msg_2", created_turn=2),
        _user_message_with_turn("msg_3", created_turn=3),
    ]
    boundary = _tail_boundary(messages, current_turn=3, recent_turn_window=1)

    assert boundary.covered_until_message_id == "msg_2"
    assert boundary.tail_start_message_id == "msg_3"


def test_tail_boundary_keeps_recent_turn_window() -> None:
    messages = [
        _user_message_with_turn("msg_1", created_turn=1),
        _user_message_with_turn("msg_2", created_turn=2),
        _user_message_with_turn("msg_3", created_turn=3),
        _user_message_with_turn("msg_4", created_turn=4),
    ]
    boundary = _tail_boundary(messages, current_turn=4, recent_turn_window=2)

    assert boundary.covered_until_message_id == "msg_2"
    assert boundary.tail_start_message_id == "msg_3"


def test_tail_boundary_rejects_when_entire_conversation_is_recent() -> None:
    messages = [
        _user_message_with_turn("msg_1", created_turn=1),
        _user_message_with_turn("msg_2", created_turn=2),
    ]

    with pytest.raises(NoSummaryError, match="no dialogue outside the recent turn window"):
        _tail_boundary(messages, current_turn=2, recent_turn_window=2)


def _user_message_with_turn(message_id: str, *, created_turn: int) -> AgentMessage:
    message = _message(message_id, "user", "内容")
    message.parts[0].metadata["created_turn"] = created_turn
    return message


def _message(message_id: str, role: str, content: str, *, created_turn: int | None = None) -> AgentMessage:
    metadata = {"created_turn": created_turn} if created_turn is not None else {}
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="text",
                content=content,
                metadata=metadata,
            )
        ],
    )


def _assistant_tool_call(message_id: str, tool_call_id: str, *, created_turn: int | None = None) -> AgentMessage:
    metadata = {"tool_call_id": tool_call_id, "tool_name": "grep"}
    if created_turn is not None:
        metadata["created_turn"] = created_turn
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="tool_call",
                content="{}",
                metadata=metadata,
            )
        ],
    )


def _tool_result(message_id: str, tool_call_id: str, *, created_turn: int | None = None) -> AgentMessage:
    metadata = {"tool_call_id": tool_call_id, "tool_name": "grep"}
    if created_turn is not None:
        metadata["created_turn"] = created_turn
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="tool",
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="tool_result",
                content="结果",
                metadata=metadata,
            )
        ],
    )
