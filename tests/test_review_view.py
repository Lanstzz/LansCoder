"""Rendering for pre-write diff review cards."""

from __future__ import annotations

from firstcoder.app.review_view import render_prewrite_review, review_command_from_text
from firstcoder.providers.types import ToolCall
from firstcoder.tools.review import build_prewrite_review


def test_review_card_highlights_additions_and_removals(tmp_path) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    review = build_prewrite_review(
        tmp_path,
        ToolCall(
            id="call_edit",
            name="edit",
            arguments={"path": "app.py", "old": "old", "new": "new"},
        ),
    )

    rendered = render_prewrite_review(review.to_payload(), expanded_paths={"app.py"})

    assert "Review before writing · 1 file · +1 -1" in rendered.plain
    assert "MODIFY  app.py · +1 -1" in rendered.plain
    assert "-old" in rendered.plain
    assert "+new" in rendered.plain
    assert any(span.style == "#c85f5f" for span in rendered.spans)
    assert any(span.style == "#7bba55" for span in rendered.spans)


def test_review_card_collapses_extra_files_and_reports_truncation(tmp_path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old\n", encoding="utf-8")
    second.write_text("old\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: first.txt
@@
-old
+new
*** Update File: second.txt
@@
-old
+new
*** End Patch"""
    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_patch", name="apply_patch", arguments={"patch": patch}),
    )

    rendered = render_prewrite_review(
        review.to_payload(),
        expanded_paths={"first.txt"},
        max_diff_lines_per_file=3,
    )

    assert "MODIFY  first.txt · +1 -1" in rendered.plain
    assert "MODIFY  second.txt · +1 -1 · collapsed" in rendered.plain
    assert "… 2 diff lines hidden" in rendered.plain
    assert "review all" in rendered.plain


def test_review_card_can_collapse_every_file(tmp_path) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_edit", name="edit", arguments={"path": "app.py", "old": "old", "new": "new"}),
    )

    rendered = render_prewrite_review(review.to_payload(), expanded_paths=set(), expand_first=False)

    assert "MODIFY  app.py · +1 -1 · collapsed" in rendered.plain
    assert "+new" not in rendered.plain


def test_review_commands_select_known_paths_and_expansion_modes() -> None:
    payload = {"files": [{"path": "app.py"}, {"path": "README.md"}]}

    assert review_command_from_text("review all", payload) == ("all", None)
    assert review_command_from_text("review clear", payload) == ("clear", None)
    assert review_command_from_text("review app.py", payload) == ("show", "app.py")
    assert review_command_from_text("review missing.py", payload) is None


def test_review_card_surfaces_planning_error() -> None:
    rendered = render_prewrite_review(
        {
            "tool_name": "edit",
            "files": [],
            "summary": {"added_lines": 0, "removed_lines": 0},
            "error": "文件已变化",
        }
    )

    assert "Preview unavailable: 文件已变化" in rendered.plain
    assert any(span.style == "#c85f5f bold" for span in rendered.spans)
