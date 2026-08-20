from __future__ import annotations

from dataclasses import dataclass

from lanscoder.context.models import SessionView


@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:

    l2_result_target_tokens: int = 800
    large_tool_result_tokens: int = 1_200
    max_turn_tool_result_tokens: int = 4_000
    max_tail_messages: int = 120
    recent_turn_window: int = 10
    cold_preview_chars: int = 160


@dataclass(frozen=True, slots=True)
class ContextTriggerDecision:
    should_compact: bool
    reason: str
    estimated_tokens: int
    target_tokens: int


def evaluate_context_triggers(
    view: SessionView,
    config: ContextCompactionConfig,
    *,
    input_tokens: int,
    high_watermark: int,
    low_watermark: int,
) -> ContextTriggerDecision:

    del view, config
    if input_tokens >= high_watermark:
        return ContextTriggerDecision(True, "token_threshold", input_tokens, low_watermark)
    return ContextTriggerDecision(False, "under_threshold", input_tokens, low_watermark)
