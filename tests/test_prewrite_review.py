"""Trusted previews for direct local file mutations."""

from __future__ import annotations

from firstcoder.providers.types import ToolCall
from firstcoder.tools.review import build_prewrite_review


def test_write_review_describes_new_file_without_writing(tmp_path) -> None:
    review = build_prewrite_review(
        tmp_path,
        ToolCall(
            id="call_write",
            name="write",
            arguments={"path": "notes/todo.txt", "content": "first\nsecond\n"},
        ),
    )

    assert review.ok is True
    assert review.summary.created_files == 1
    assert review.summary.added_lines == 2
    assert review.files[0].operation == "create"
    assert review.files[0].path == "notes/todo.txt"
    assert "+first" in review.files[0].diff
    assert not (tmp_path / "notes" / "todo.txt").exists()


def test_edit_review_reports_replacement_without_writing(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 'old'\n", encoding="utf-8")

    review = build_prewrite_review(
        tmp_path,
        ToolCall(
            id="call_edit",
            name="edit",
            arguments={"path": "app.py", "old": "'old'", "new": "'new'"},
        ),
    )

    assert review.ok is True
    assert review.summary.modified_files == 1
    assert review.summary.removed_lines == 1
    assert review.summary.added_lines == 1
    assert "-value = 'old'" in review.files[0].diff
    assert "+value = 'new'" in review.files[0].diff
    assert target.read_text(encoding="utf-8") == "value = 'old'\n"


def test_apply_patch_review_describes_move_and_delete_without_writing(tmp_path) -> None:
    (tmp_path / "old.py").write_text("name = 'old'\n", encoding="utf-8")
    (tmp_path / "remove.txt").write_text("gone\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: old.py
*** Move to: src/new.py
@@
-name = 'old'
+name = 'new'
*** Delete File: remove.txt
*** End Patch"""

    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_patch", name="apply_patch", arguments={"patch": patch}),
    )

    assert review.ok is True
    assert review.summary.moved_files == 1
    assert review.summary.deleted_files == 1
    assert [(item.path, item.operation) for item in review.files] == [
        ("src/new.py", "move"),
        ("remove.txt", "delete"),
    ]
    assert review.files[0].source_path == "old.py"
    assert "-name = 'old'" in review.files[0].diff
    assert "+name = 'new'" in review.files[0].diff
    assert not (tmp_path / "src" / "new.py").exists()
    assert (tmp_path / "old.py").exists()
    assert (tmp_path / "remove.txt").exists()


def test_delete_review_contains_removed_content_without_deleting(tmp_path) -> None:
    target = tmp_path / "remove.txt"
    target.write_text("gone\n", encoding="utf-8")

    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_delete", name="delete", arguments={"path": "remove.txt"}),
    )

    assert review.ok is True
    assert review.summary.deleted_files == 1
    assert review.files[0].operation == "delete"
    assert "-gone" in review.files[0].diff
    assert target.exists()


def test_edit_review_rejects_non_unique_replacement(tmp_path) -> None:
    (tmp_path / "app.py").write_text("old\nold\n", encoding="utf-8")

    review = build_prewrite_review(
        tmp_path,
        ToolCall(
            id="call_edit",
            name="edit",
            arguments={"path": "app.py", "old": "old", "new": "new"},
        ),
    )

    assert review.ok is False
    assert review.error == "匹配内容出现 2 次；请提供更精确的 old，或启用 replace_all"


def test_review_supports_each_direct_mutation_tool_but_not_shell(tmp_path) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: app.py
@@
-old
+new
*** End Patch"""
    calls = [
        ToolCall(id="write", name="write", arguments={"path": "new.txt", "content": "new\n"}),
        ToolCall(id="edit", name="edit", arguments={"path": "app.py", "old": "old", "new": "new"}),
        ToolCall(id="patch", name="apply_patch", arguments={"patch": patch}),
        ToolCall(id="delete", name="delete", arguments={"path": "app.py"}),
    ]

    assert all(build_prewrite_review(tmp_path, call).ok for call in calls)
    shell_review = build_prewrite_review(
        tmp_path,
        ToolCall(id="shell", name="shell", arguments={"command": "printf changed > app.py"}),
    )
    assert shell_review.ok is False
    assert shell_review.error == "工具 shell 不支持写前预览"


def test_apply_patch_review_rejects_repeated_paths(tmp_path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: app.py
@@
-one
+two
*** Update File: app.py
@@
-two
+three
*** End Patch"""

    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_patch", name="apply_patch", arguments={"patch": patch}),
    )

    assert review.ok is False
    assert review.error == "patch 不能重复修改同一路径：app.py"


def test_delete_review_describes_binary_file_without_reading_it_as_text(tmp_path) -> None:
    target = tmp_path / "image.bin"
    target.write_bytes(b"\xff\x00\xfe")

    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_delete", name="delete", arguments={"path": "image.bin"}),
    )

    assert review.ok is True
    assert review.files[0].operation == "delete"
    assert review.files[0].path == "image.bin"
    assert "Binary file will be deleted" in review.files[0].diff
    assert review.is_current(tmp_path) is True
    target.write_bytes(b"changed")
    assert review.is_current(tmp_path) is False


def test_recursive_delete_review_does_not_follow_symlink_contents(tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("do not expose this secret", encoding="utf-8")
    target = tmp_path / "cache"
    target.mkdir()
    (target / "secret-link").symlink_to(outside)

    review = build_prewrite_review(
        tmp_path,
        ToolCall(
            id="call_delete",
            name="delete",
            arguments={"path": "cache", "recursive": True},
        ),
    )

    assert review.ok is True
    assert "do not expose this secret" not in review.files[1].diff
    assert "Symbolic link will be deleted" in review.files[1].diff
    outside.write_text("changed outside content", encoding="utf-8")
    assert review.is_current(tmp_path) is True


def test_single_symlink_delete_review_tracks_link_not_target_contents(tmp_path) -> None:
    first_target = tmp_path / "first-target.txt"
    first_target.write_text("first", encoding="utf-8")
    second_target = tmp_path / "second-target.txt"
    second_target.write_text("second", encoding="utf-8")
    link = tmp_path / "target-link"
    link.symlink_to(first_target)

    review = build_prewrite_review(
        tmp_path,
        ToolCall(id="call_delete", name="delete", arguments={"path": "target-link"}),
    )

    assert review.ok is True
    assert review.is_current(tmp_path) is True
    first_target.write_text("changed target contents", encoding="utf-8")
    assert review.is_current(tmp_path) is True
    link.unlink()
    link.symlink_to(second_target)
    assert review.is_current(tmp_path) is False
