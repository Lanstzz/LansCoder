from __future__ import annotations

from lanscoder.app.activity_view import single_line_activity
from lanscoder.app.tui_state import (
    BlockKind,
    ChildItem,
    ChildKind,
    TranscriptBlock,
    TuiEntryKind,
    TuiTranscriptEntry,
)


def looks_like_markdown_response(line: str) -> bool:
    return not looks_like_tool_display_line(line)


def looks_like_tool_display_line(line: str) -> bool:
    return line.startswith(("Tool call:", "Tool result:"))


def normalize_stream_text(text: str) -> str:
    return text.strip()


def display_line_kind(line: str) -> TuiEntryKind:
    if line.startswith(("Tool call:", "Tool result:")):
        return TuiEntryKind.TOOL
    return TuiEntryKind.SYSTEM


def display_line_status(line: str) -> str | None:
    if line.startswith("Tool call:"):
        return "running"
    if line.startswith("Tool result:"):
        return "success"
    return None


def entry_classes(entry: TuiTranscriptEntry) -> str:
    base = "message"
    if entry.kind == TuiEntryKind.SYSTEM:
        return f"{base} system-message"
    if entry.kind == TuiEntryKind.COMMAND:
        return f"{base} command-message"
    if entry.kind == TuiEntryKind.USER:
        return f"{base} user-message"
    if entry.kind == TuiEntryKind.ASSISTANT:
        return f"{base} assistant-message"
    if entry.kind == TuiEntryKind.REASONING:
        return f"{base} reasoning-message"
    if entry.kind == TuiEntryKind.PERMISSION:
        if entry.status == "permission_requested":
            return f"{base} permission-message permission-requested"
        return f"{base} permission-message"
    if entry.kind == TuiEntryKind.ERROR:
        return f"{base} error-message"
    if entry.kind == TuiEntryKind.TOOL:
        if entry.status == "running":
            return f"{base} tool-message tool-running"
        if entry.status == "success":
            return f"{base} tool-message tool-done"
        if entry.status in {"error", "denied", "failed"}:
            return f"{base} tool-message tool-failed"
        return f"{base} tool-message"
    return f"{base} system-message"


def entry_plain_text(entry: TuiTranscriptEntry) -> str:
    if entry.kind in {TuiEntryKind.USER, TuiEntryKind.ASSISTANT, TuiEntryKind.TOOL, TuiEntryKind.REASONING}:
        return f"{entry.label}\n  {entry.body}"
    return entry.body


def entry_markdown_text(entry: TuiTranscriptEntry) -> str:
    return f"{entry.label}\n\n{entry.body}"


def tool_event_entry_kind(event) -> TuiEntryKind:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "permission_requested":
        return TuiEntryKind.PERMISSION
    return TuiEntryKind.TOOL


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
    status_class = {"running": "tool-running", "success": "tool-done", "error": "tool-failed", "denied": "tool-denied"}.get(
        child.status or "", ""
    )
    return f"child-row child-tool {status_class}".rstrip()
