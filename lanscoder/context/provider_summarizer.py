"""用 provider 实现 L3 checkpoint 摘要的适配器。"""

from __future__ import annotations

import re

from lanscoder.context.llm_compact import (
    DIALOGUE_SUMMARY_HEADINGS,
    CompactTimeoutError,
    LlmCompactSummarizer,
    LlmCompactSummary,
    NoSummaryError,
    PromptTooLongError,
    normalize_coding_handoff,
)
from lanscoder.context.models import AgentMessage
from lanscoder.context.tool_sequence import InvalidToolCallSequenceError, validate_tool_call_sequence
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.errors import ProviderError, ProviderErrorKind
from lanscoder.providers.types import ChatMessage, ChatRequest


class ProviderLlmCompactSummarizer(LlmCompactSummarizer):
    """把上下文层的 L3 summarizer 协议适配到通用 `ChatProvider`。

    L3 checkpoint 需要三个事实：摘要正文、被摘要覆盖到哪里、从哪里开始保留 tail。
    默认实现只让模型生成摘要正文，边界由本地根据当前消息序列选择并继续交给
    `LlmCompactService` 校验，避免把恢复边界完全交给模型决定。
    """

    def __init__(self, provider: ChatProvider, *, max_tokens: int = 1200) -> None:
        self.provider = provider
        self.max_tokens = max_tokens

    def summarize(
        self,
        messages: list[AgentMessage],
        *,
        summary_mode: str = "default",
        current_turn: int = 0,
        recent_turn_window: int = 10,
    ) -> LlmCompactSummary:
        tail = _tail_boundary(messages, current_turn=current_turn, recent_turn_window=recent_turn_window)
        prompt = _build_dialogue_summary_prompt(messages, summary_mode=summary_mode)
        try:
            response = self.provider.complete(
                ChatRequest(
                    messages=[
                        ChatMessage(
                            role="system",
                            content=(
                                "你是 LansCoder 的上下文压缩器。输出简洁的多轮对话摘要；"
                                "必须且只能使用指定的四个 Markdown 标题，每个恰好一次；"
                                "只在标题下写有证据支持的事实。不要选择 checkpoint 边界。"
                            ),
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    tools=[],
                    tool_choice="none",
                    max_tokens=self.max_tokens,
                )
            )
        except ProviderError as error:
            if error.kind == ProviderErrorKind.PROMPT_TOO_LONG:
                raise PromptTooLongError(str(error)) from error
            if error.kind == ProviderErrorKind.TIMEOUT:
                raise CompactTimeoutError(str(error)) from error
            raise NoSummaryError(str(error)) from error
        summary = response.content.strip()
        if not summary:
            raise NoSummaryError("empty summary")
        return LlmCompactSummary(
            summary=normalize_coding_handoff(summary, headings=DIALOGUE_SUMMARY_HEADINGS),
            tail_start_message_id=tail.tail_start_message_id,
            covered_until_message_id=tail.covered_until_message_id,
        )


class _TailBoundary:
    def __init__(self, *, tail_start_message_id: str, covered_until_message_id: str) -> None:
        self.tail_start_message_id = tail_start_message_id
        self.covered_until_message_id = covered_until_message_id


def _tail_boundary(
    messages: list[AgentMessage],
    *,
    current_turn: int,
    recent_turn_window: int,
) -> _TailBoundary:
    """选择保守 tail，同时保证保留最近 N 轮对话。"""

    candidates = _boundary_candidates(messages)
    if len(candidates) < 2:
        raise NoSummaryError("not enough messages to summarize")

    max_tail_start_index = _recent_turn_max_tail_start_index(
        candidates,
        current_turn=current_turn,
        recent_turn_window=recent_turn_window,
    )
    if max_tail_start_index <= 0:
        raise NoSummaryError("no dialogue outside the recent turn window")

    for index in range(max_tail_start_index, 0, -1):
        try:
            validate_tool_call_sequence(candidates[index:])
        except InvalidToolCallSequenceError:
            continue
        return _TailBoundary(
            tail_start_message_id=candidates[index].id,
            covered_until_message_id=candidates[index - 1].id,
        )
    raise NoSummaryError("could not find a valid checkpoint tail boundary")


def select_compaction_boundary(
    messages: list[AgentMessage],
    *,
    current_turn: int,
    recent_turn_window: int,
) -> tuple[str, str] | None:
    """Return (covered_until_message_id, tail_start_message_id) for hard truncation.

    Mirrors `_tail_boundary`: respects the recent-N window and tool-call sequence
    validity. Returns None when the whole conversation is inside the window (nothing
    to drop) or no valid boundary exists — the manager then falls through to failure.
    """

    try:
        boundary = _tail_boundary(
            messages,
            current_turn=current_turn,
            recent_turn_window=recent_turn_window,
        )
    except NoSummaryError:
        return None
    return boundary.covered_until_message_id, boundary.tail_start_message_id


def _recent_turn_max_tail_start_index(
    candidates: list[AgentMessage],
    *,
    current_turn: int,
    recent_turn_window: int,
) -> int:
    """tail_start 允许的最大候选下标。

    保留最近 N 轮意味着 tail 必须包含 turn [T-N+1, T] 的全部消息，所以
    tail_start 最晚必须落在 turn T-N+1 开头的消息上。若 T-N+1 <= 1（全部
    都在窗口内）或历史不足，返回 0（无可摘要）。
    """

    min_created_turn = current_turn - recent_turn_window + 1
    if min_created_turn <= 1:
        return 0
    for index, message in enumerate(candidates):
        turn = _message_created_turn(message)
        if turn is None:
            continue
        if turn >= min_created_turn:
            return index
    return 0


def _message_created_turn(message: AgentMessage) -> int | None:
    for part in message.parts:
        created_turn = part.metadata.get("created_turn")
        if isinstance(created_turn, int) and not isinstance(created_turn, bool):
            return created_turn
    return None


def _boundary_candidates(messages: list[AgentMessage]) -> list[AgentMessage]:
    return [message for message in messages if not any(part.kind == "checkpoint_summary" for part in message.parts)]


def _build_dialogue_summary_prompt(messages: list[AgentMessage], *, summary_mode: str) -> str:
    mode_hint = "更强压缩，优先保留事实与约束。" if summary_mode == "stronger" else "常规压缩。"
    headings = "\n".join(DIALOGUE_SUMMARY_HEADINGS)
    sections = [
        f"摘要模式：{mode_hint}",
        "",
        "以下会话的多轮对话需要压缩。只输出一次下面的每个标题；若该项没有证据，写“无”：",
        headings,
        "",
        "要求：保留用户请求的要点、已经给出的结论、尚未完成或悬而未决的事项、以及影响后续工作的关键约束与偏好。丢弃：工具调用细节、中间推理、重复信息。",
        "",
        "需要压缩的对话历史：",
    ]
    for message in messages:
        content = _message_text(message)
        if not content:
            continue
        sections.append(f"\n[{message.id}] role={message.role}\n{content}")
    return "\n".join(sections)


def _message_text(message: AgentMessage) -> str:
    chunks = [part.content for part in message.parts if part.content]
    return _collapse_text("\n".join(chunks))


def _collapse_text(value: str, *, max_chars: int = 4000) -> str:
    collapsed = re.sub(r"\n{3,}", "\n\n", value.strip())
    if len(collapsed) <= max_chars:
        return collapsed
    return f"{collapsed[:max_chars]}\n...[truncated]"
