"""确定性内容压缩器。

这一层只做可预测的轻量替换，不调用 LLM。目标是先降低 token 占用，并用 metadata
记录压缩状态，避免后续 pipeline 对同一 part 反复处理。
"""

from __future__ import annotations

from lanscoder.context.content.router import (
    RouteCompactResult,
    RouteContentType,
    RouteContext,
)
from lanscoder.context.models import MessagePart
from lanscoder.context.token_budget import estimate_text_tokens


class PlainTextRouteCompressor:
    """派生工具输出的确定性 fallback compressor。

    专用 search、diff、build、json、code、html compressor 不适用时，保留首尾和
    明确的 token 元数据。它只由 L2 的 tool-result 路由调用，不能用于普通对话或
    fresh source read。
    """

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        preview = part.content[: context.preview_chars]
        tail_preview = part.content[-context.preview_chars :] if len(part.content) > context.preview_chars else ""
        preview_tokens = estimate_text_tokens(preview)
        tail_preview_tokens = estimate_text_tokens(tail_preview) if tail_preview else 0
        content = "\n".join(
            [
                "[Derived tool result compacted]",
                f"part_id={part.id}",
                f"original_tokens={estimate_text_tokens(part.content)}",
                f"preview_tokens={preview_tokens}",
                f"preview={preview}",
                f"tail_preview_tokens={tail_preview_tokens}",
                f"tail_preview={tail_preview}",
            ]
        )
        return RouteCompactResult(
            content=content,
            content_type=RouteContentType.PLAIN_TEXT,
            compacted_by="l1_current_task_cold",
            metadata={
                "preview": preview,
                "preview_tokens": preview_tokens,
                "tail_preview": tail_preview,
                "tail_preview_tokens": tail_preview_tokens,
            },
        )
