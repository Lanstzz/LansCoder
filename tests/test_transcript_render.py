from lanscoder.app.transcript_view import (
    block_classes,
    child_collapsed_text,
    child_expanded_text,
    child_row_classes,
)
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock


def test_block_classes_map_kinds():
    assert block_classes(TranscriptBlock(BlockKind.USER)) == "message user-message"
    assert block_classes(TranscriptBlock(BlockKind.ASSISTANT)) == "message assistant-message"
    assert block_classes(TranscriptBlock(BlockKind.SYSTEM)) == "message system-message"
    assert block_classes(TranscriptBlock(BlockKind.COMMAND)) == "message command-message"
    assert block_classes(TranscriptBlock(BlockKind.ERROR)) == "message error-message"


def test_thinking_collapsed_shows_thinking_and_preview():
    child = ChildItem(ChildKind.THINKING, "t0", "Thinking…", body="核心在 session 校验")
    assert child_collapsed_text(child).startswith("◎ Thinking…")


def test_thinking_collapsed_without_body_shows_label_only():
    child = ChildItem(ChildKind.THINKING, "t0", "Thinking…")
    assert child_collapsed_text(child) == "◎ Thinking…"


def test_tool_collapsed_status_suffixes():
    running = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="running")
    done = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="success")
    failed = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="error")
    denied = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="denied")
    assert child_collapsed_text(running) == "[>] tool read auth.py · running"
    assert child_collapsed_text(done) == "[>] tool read auth.py ✓"
    assert child_collapsed_text(failed) == "[>] tool read auth.py ✗"
    assert child_collapsed_text(denied) == "[>] tool read auth.py ✕"


def test_child_expanded_content():
    thinking = ChildItem(ChildKind.THINKING, "t0", "Thinking…", body="full\nbody")
    tool = ChildItem(ChildKind.TOOL, "c1", "tool read a.py", status="success", body="result 200")
    assert child_expanded_text(thinking) == "full\nbody"
    assert child_expanded_text(tool) == "tool: tool read a.py\nresult 200"


def test_child_row_classes_by_kind_and_status():
    thinking = ChildItem(ChildKind.THINKING, "t0", "Thinking…")
    done = ChildItem(ChildKind.TOOL, "c1", "tool read", status="success")
    assert child_row_classes(thinking) == "child-row child-thinking"
    assert child_row_classes(done) == "child-row child-tool tool-done"
