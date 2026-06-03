"""session metadata 处理。

metadata 是用户可见的 session 摘要信息，不应该变成普通 conversation message。
这里保留用户可见标题策略；metadata patch 合并函数放在 context 侧，避免
`context.store` 反向依赖 session 层。
"""

from __future__ import annotations

from firstcoder.context.metadata import merge_metadata_patch


DEFAULT_TITLE_CHARS = 40


def title_from_first_user_message(content: str | None, *, max_chars: int = DEFAULT_TITLE_CHARS) -> str | None:
    """从第一条用户消息生成保守标题。"""

    if content is None:
        return None
    normalized = " ".join(content.split())
    if not normalized:
        return None
    if max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    ellipsis = "..."
    if max_chars <= len(ellipsis):
        return ellipsis[:max_chars]
    return normalized[: max_chars - len(ellipsis)] + ellipsis
