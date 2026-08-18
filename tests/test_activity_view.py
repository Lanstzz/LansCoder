"""Activity-line rendering helper tests."""

from rich.text import Text

from firstcoder.app.activity_view import single_line_activity, truncate_activity_text


def test_single_line_activity_folds_newlines_but_keeps_animation_spaces() -> None:
    assert (
        single_line_activity("thinking ⠋ 好的，我来分析。\n先看下目录结构")
        == "thinking ⠋ 好的，我来分析。 先看下目录结构"
    )
    assert single_line_activity("  a   b\n\nc ") == "a   b  c"
    assert single_line_activity("running [=   ] · echo\nnext") == "running [=   ] · echo next"


def test_truncate_activity_text_returns_text_unchanged_when_it_fits() -> None:
    assert truncate_activity_text("abc", 5) == "abc"


def test_truncate_activity_text_collapses_multiline_before_truncating() -> None:
    result = truncate_activity_text("好的，我来分析。\n先看下目录结构", 10)

    assert "\n" not in result
    assert Text(result).cell_len <= 10
    assert result.endswith(".")


def test_truncate_activity_text_truncates_by_cell_width_not_char_count() -> None:
    # 中文按 2 列渲染：宽度 10 只允许约 9 列文本 + 一个 "."。
    result = truncate_activity_text("好的，我来分析。先看下目录结构", 10)

    assert Text(result).cell_len <= 10
    assert result.endswith(".")


def test_truncate_activity_text_handles_empty_and_tiny_widths() -> None:
    assert truncate_activity_text("", 10) == ""
    assert truncate_activity_text("abc", 1) == "a"
    assert truncate_activity_text("abc", 0) == ""
