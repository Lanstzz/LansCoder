"""Provider usage detail and streaming merge behavior for observability."""

from __future__ import annotations

import json
import asyncio

from lanscoder.providers.anthropic_provider import AnthropicProvider
from lanscoder.providers.openai_compatible import OpenAICompatibleProvider
from lanscoder.providers.streaming import extract_usage_details, merge_usage, token_usage
from lanscoder.providers.types import ChatMessage, ChatRequest, TokenUsage


class _UsageObject:
    def __init__(self, **values):
        self.__dict__.update(values)


class _OpenAIUsageCompletions:
    def create(self, **params):
        return _UsageObject(
            model=params["model"],
            usage=_UsageObject(
                prompt_tokens=11,
                completion_tokens=7,
                total_tokens=18,
                prompt_tokens_details=_UsageObject(cached_tokens=4, audio_tokens=1, ignored="metadata"),
                completion_tokens_details={"reasoning_tokens": 3, "audio_tokens": 2},
            ),
            choices=[
                _UsageObject(
                    finish_reason="stop",
                    message=_UsageObject(content="done", tool_calls=[]),
                )
            ],
        )


class _OpenAIUsageClient:
    def __init__(self):
        self.chat = _UsageObject(completions=_OpenAIUsageCompletions())


class _AnthropicUsageMessages:
    def create(self, **params):
        return _UsageObject(
            model=params["model"],
            stop_reason="end_turn",
            usage=_UsageObject(
                input_tokens=20,
                output_tokens=8,
                cache_creation_input_tokens=12,
                cache_read_input_tokens=9,
            ),
            content=[_UsageObject(type="text", text="done")],
        )


class _AnthropicUsageClient:
    def __init__(self):
        self.messages = _AnthropicUsageMessages()


def test_openai_chat_response_contains_usage_details() -> None:
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=_OpenAIUsageClient(),
    )

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert response.usage is not None
    assert response.usage.usage_details == {
        "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 1},
        "completion_tokens_details": {"reasoning_tokens": 3, "audio_tokens": 2},
    }


def test_anthropic_chat_response_contains_cache_usage_details() -> None:
    provider = AnthropicProvider(
        model="claude-test",
        api_key="test-key",
        client=_AnthropicUsageClient(),
    )

    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert response.usage is not None
    assert response.usage.usage_details == {
        "cache_creation_input_tokens": 12,
        "cache_read_input_tokens": 9,
    }


def test_token_usage_keeps_only_json_safe_details() -> None:
    details = {
        "prompt_tokens_details": {
            "cached_tokens": 4,
            "unsupported": object(),
        },
        "completion_tokens_details": {"reasoning_tokens": 3},
        "unsupported": object(),
    }

    usage = TokenUsage(usage_details=details)

    assert usage.usage_details == {
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    json.dumps(usage.usage_details)


def test_extract_usage_details_preserves_openai_integer_detail_fields() -> None:
    provider_usage = _UsageObject(
        prompt_tokens_details=_UsageObject(cached_tokens=7, audio_tokens=2, label="ignored"),
        completion_tokens_details={"reasoning_tokens": 5, "audio_tokens": 1, "label": "ignored"},
    )

    details = extract_usage_details(
        provider_usage,
        nested_fields=("prompt_tokens_details", "completion_tokens_details"),
    )

    assert details == {
        "prompt_tokens_details": {"cached_tokens": 7, "audio_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 5, "audio_tokens": 1},
    }


def test_extract_usage_details_preserves_anthropic_cache_usage() -> None:
    provider_usage = _UsageObject(
        input_tokens=20,
        output_tokens=8,
        cache_creation_input_tokens=12,
        cache_read_input_tokens=9,
        ignored="metadata",
    )

    details = extract_usage_details(
        provider_usage,
        scalar_fields=("cache_creation_input_tokens", "cache_read_input_tokens"),
    )

    assert details == {
        "cache_creation_input_tokens": 12,
        "cache_read_input_tokens": 9,
    }


def test_streaming_usage_merges_cumulative_snapshots_without_double_counting() -> None:
    snapshots = [
        token_usage(
            10,
            None,
            usage_details={"prompt_tokens_details": {"cached_tokens": 2}},
        ),
        token_usage(
            None,
            4,
            14,
            usage_details={"prompt_tokens_details": {"cached_tokens": 6}},
        ),
        token_usage(
            10,
            4,
            14,
            usage_details={"prompt_tokens_details": {}},
        ),
    ]

    merged = None
    for snapshot in snapshots:
        merged = merge_usage(merged, snapshot)

    assert merged == TokenUsage(
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
        usage_details={"prompt_tokens_details": {"cached_tokens": 6}},
    )


def test_streaming_usage_adds_only_explicit_deltas() -> None:
    merged = merge_usage(
        TokenUsage(
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
            usage_details={"prompt_tokens_details": {"cached_tokens": 2}},
        ),
        TokenUsage(
            input_tokens=3,
            output_tokens=2,
            total_tokens=5,
            usage_details={
                "prompt_tokens_details": {"cached_tokens": 1},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        ),
        right_is_delta=True,
    )

    assert merged == TokenUsage(
        input_tokens=13,
        output_tokens=6,
        total_tokens=19,
        usage_details={
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    )


def test_openai_streaming_preserves_cumulative_usage_chunks() -> None:
    class StreamCompletions:
        def create(self, **params):
            return iter(
                [
                    _UsageObject(model="test-model", choices=[], usage=_UsageObject(prompt_tokens=4)),
                    _UsageObject(
                        model="test-model",
                        choices=[
                            _UsageObject(
                                delta=_UsageObject(content="done"),
                                finish_reason="stop",
                            )
                        ],
                        usage=_UsageObject(prompt_tokens=4, completion_tokens=1, total_tokens=5),
                    ),
                ]
            )

    client = _UsageObject(chat=_UsageObject(completions=StreamCompletions()))
    provider = OpenAICompatibleProvider(
        name="test-openai",
        model="test-model",
        api_key="test-key",
        client=client,
    )

    async def collect():
        return [event async for event in provider.astream(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))]

    events = asyncio.run(collect())
    response = events[-1].response
    assert response is not None
    assert response.usage == TokenUsage(input_tokens=4, output_tokens=1, total_tokens=5)


def test_partial_cumulative_snapshot_refreshes_derived_total():
    merged = merge_usage(token_usage(10, 0), token_usage(None, 4))
    assert merged == TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14)


def test_partial_explicit_delta_updates_total_without_readding_input():
    merged = merge_usage(token_usage(10, 4), token_usage(None, 2), right_is_delta=True)
    assert merged == TokenUsage(input_tokens=10, output_tokens=6, total_tokens=16)
