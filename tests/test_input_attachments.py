"""attachments 粘贴解析的崩溃回归测试。

回归对象:Path.expanduser 对无法解析的 ``~user`` 前缀抛 RuntimeError
(``RuntimeError: Could not determine home directory.``),此前会让粘贴文本含
``~somebody/path`` 这类 token 时整段炸掉 TUI 事件处理器。
"""

from __future__ import annotations

import pytest

from lanscoder.input.attachments import (
    _path_from_candidate,
    attach_path,
    resolve_paste_attachments,
)

UNRESOLVABLE_USER = "no_such_user_lanscoder_rj42"


@pytest.mark.parametrize(
    "paste",
    [
        f"prefix\n~{UNRESOLVABLE_USER}/notes.txt\nsuffix",
        f"prefix\n~{UNRESOLVABLE_USER}\nsuffix",
        f"code ~{UNRESOLVABLE_USER}/x.py here",
    ],
)
def test_paste_with_unresolvable_tilde_does_not_crash(paste: str) -> None:
    attachments = resolve_paste_attachments(paste, include_clipboard_image=False)
    assert attachments == []


@pytest.mark.parametrize(
    "candidate",
    [
        f"~{UNRESOLVABLE_USER}/notes.txt",
        f"~{UNRESOLVABLE_USER}",
    ],
)
def test_path_from_candidate_unresolvable_tilde_returns_none(candidate: str) -> None:
    assert _path_from_candidate(candidate) is None


def test_attach_path_unresolvable_tilde_raises_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        attach_path(f"~{UNRESOLVABLE_USER}/notes.txt")


def test_paste_with_existing_file_path_still_attaches(tmp_path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    attachments = resolve_paste_attachments(str(notes), include_clipboard_image=False)
    assert len(attachments) == 1
    assert attachments[0].path == notes.resolve()
