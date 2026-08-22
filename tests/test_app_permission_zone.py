from types import SimpleNamespace

from lanscoder.app.permission_view import (
    permission_choice_for_text,
    permission_options_text,
)

_PREWRITE_PAYLOAD = {
    "prewrite_review": {
        "tool_name": "edit",
        "files": [
            {
                "path": "README.md",
                "operation": "modify",
                "diff": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new",
                "added_lines": 1,
                "removed_lines": 1,
            }
        ],
        "summary": {"added_lines": 1, "removed_lines": 1},
        "error": None,
    }
}


def _pending(options, *, payload=None):
    return SimpleNamespace(
        id="req-1",
        kind="permission_confirmation",
        question="允许吗？",
        options=[SimpleNamespace(id=oid, label=olabel) for oid, olabel in options],
        payload=payload or {},
    )


def test_digits_map_to_options_by_position():
    pending = _pending([("deny", "Deny"), ("allow_once", "Allow once"), ("allow_always_same_scope", "Allow always")])
    assert permission_choice_for_text("1", pending) == "deny"
    assert permission_choice_for_text("2", pending) == "allow_once"
    assert permission_choice_for_text("3", pending) == "allow_always_same_scope"


def test_number_out_of_range_is_invalid():
    pending = _pending([("deny", "Deny"), ("allow_once", "Allow once")])
    assert permission_choice_for_text("3", pending) is None
    assert permission_choice_for_text("0", pending) is None
    assert permission_choice_for_text("4", pending) is None


def test_text_aliases_are_rejected():
    pending = _pending([("deny", "Deny"), ("allow_once", "Allow once"), ("allow_always_same_scope", "Allow always")])
    for word in ("deny", "allow", "allow once", "always", "no", "reject", "y", "OK"):
        assert permission_choice_for_text(word, pending) is None


def test_non_numeric_and_surrounding_whitespace():
    pending = _pending([("deny", "Deny"), ("allow_once", "Allow once")])
    assert permission_choice_for_text(" 2 ", pending) == "allow_once"
    assert permission_choice_for_text("2.0", pending) is None
    assert permission_choice_for_text("", pending) is None


def test_reject_feedback_only_for_prewrite_review():
    pending = _pending([("deny", "Deny"), ("allow_once", "Apply reviewed change")], payload=_PREWRITE_PAYLOAD)
    assert permission_choice_for_text("reject: 请保留标题", pending) == "reject_with_feedback: 请保留标题"
    assert permission_choice_for_text("reject_with_feedback: x", pending) == "reject_with_feedback: x"


def test_reject_feedback_without_prewrite_is_invalid():
    pending = _pending([("deny", "Deny"), ("allow_once", "Allow once")])
    assert permission_choice_for_text("reject: 别改", pending) is None


def test_options_text_lists_strict_numbers():
    pending = _pending([("deny", "Deny"), ("allow_once", "Allow once"), ("allow_always_same_scope", "Allow always")])
    text = permission_options_text(pending)
    assert text.startswith("只能输入 1/2/3")
    assert "[1] deny" in text
    assert "[2] allow once" in text
    assert "[3] allow always" in text


def test_options_text_mentions_reject_for_prewrite():
    pending = _pending([("deny", "Deny"), ("allow_once", "Apply reviewed change")], payload=_PREWRITE_PAYLOAD)
    hint = permission_options_text(pending)
    assert hint.startswith("只能输入 1/2")
    assert "reject: <反馈>" in hint
