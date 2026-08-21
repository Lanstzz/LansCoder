from __future__ import annotations

from lanscoder.app.activity_view import single_line_activity
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock


def looks_like_markdown_response(line: str) -> bool:
    return not looks_like_tool_display_line(line)


def looks_like_tool_display_line(line: str) -> bool:
    return line.startswith(("Tool call:", "Tool result:"))


def normalize_stream_text(text: str) -> str:
    return text.strip()


def entry_markdown_text_block(block: TranscriptBlock) -> str:
    if block.kind == BlockKind.ASSISTANT:
        return f"LansCoder:\n\n{block.text}"
    return block.text


_BLOCK_CLASSES = {
    BlockKind.USER: "message user-message",
    BlockKind.ASSISTANT: "message assistant-message",
    BlockKind.SYSTEM: "message system-message",
    BlockKind.COMMAND: "message command-message",
    BlockKind.ERROR: "message error-message",
}


def block_classes(block: TranscriptBlock) -> str:
    return _BLOCK_CLASSES.get(block.kind, "message system-message")


_TOOL_STATUS_SUFFIX = {"success": " ✓", "error": " ✗", "denied": " ✕", "running": " · running"}


def child_collapsed_text(child: ChildItem) -> str:
    if child.kind == ChildKind.THINKING:
        base = "◎ Thinking…"
        if child.body:
            return f"{base} {single_line_activity(child.body)}"
        return base
    suffix = _TOOL_STATUS_SUFFIX.get(child.status or "", "")
    return f"[>] {child.label}{suffix}"


def child_expanded_text(child: ChildItem) -> str:
    if child.kind == ChildKind.THINKING:
        return child.body or ""
    head = f"tool: {child.label}"
    body = child.body
    return f"{head}\n{body}" if body else head


def child_row_classes(child: ChildItem) -> str:
    if child.kind == ChildKind.THINKING:
        return "child-row child-thinking"
    status_class = {"running": "tool-running", "success": "tool-done", "error": "tool-failed", "denied": "tool-denied"}.get(child.status or "", "")
    return f"child-row child-tool {status_class}".rstrip()
