"""Permission prompt rendering and answer parsing for the TUI."""

from __future__ import annotations


def permission_choice_for_text(text: str, pending) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    raw = text.strip()
    if raw.lower().startswith(("reject:", "reject_with_feedback:")):
        return f"reject_with_feedback: {raw.split(':', 1)[1].strip()}"
    aliases = {
        "1": "deny",
        "no": "deny",
        "deny": "deny",
        "4": "reject_with_feedback",
        "reject": "reject_with_feedback",
        "reject_with_feedback": "reject_with_feedback",
        "2": "allow_once",
        "allow_once": "allow_once",
        "once": "allow_once",
        "allow": "allow_once",
        "3": "allow_always_same_scope",
        "allow_always": "allow_always_same_scope",
        "always": "allow_always_same_scope",
        "allow_always_same_scope": "allow_always_same_scope",
    }
    if normalized in aliases:
        return aliases[normalized]
    for option in getattr(pending, "options", []) or []:
        if normalized in {str(option.id).lower(), str(option.label).strip().lower().replace(" ", "_")}:
            return str(option.id)
    return None


def permission_options_text(pending) -> str:
    options = getattr(pending, "options", []) or []
    if not options:
        return "请回复权限选择：deny / allow_once / allow_always_same_scope"
    rendered = ", ".join(f"{option.id} ({option.label})" for option in options)
    return f"请回复权限选择：{rendered}"


def permission_prompt_text(pending) -> str:
    payload = getattr(pending, "payload", {}) or {}
    action = str(payload.get("action") or "")
    target = str(payload.get("target") or "")
    reason = str(payload.get("reason") or "")
    question = str(getattr(pending, "question", "") or "允许执行这个权限操作吗？")

    headline = "permission requested"
    if action and target:
        headline = f"{headline}  {action} {target}"
    elif action:
        headline = f"{headline}  {action}"
    elif target:
        headline = f"{headline}  {target}"
    lines = [headline]
    if reason:
        lines.append(f"  {reason}")
    elif not any((action, target)):
        lines.append(f"  {question}")

    options = list(getattr(pending, "options", []) or [])
    if options:
        choices: list[str] = []
        option_numbers = {
            "deny": 1,
            "allow_once": 2,
            "allow_always_same_scope": 3,
            "reject_with_feedback": 4,
        }
        for index, option in enumerate(options, start=1):
            label = str(getattr(option, "label", "") or getattr(option, "id", ""))
            option_id = str(getattr(option, "id", ""))
            rendered = permission_option_label(label, option_id)
            choices.append(f"[{option_numbers.get(option_id, index)}] {rendered}")
        lines.append("  " + "  ".join(choices))
    else:
        lines.append("  [1] deny  [2] allow once  [3] allow always")
    if isinstance(payload.get("prewrite_review"), dict):
        lines.append("  Or reply: reject: <feedback>")
    return "\n".join(lines)


def permission_option_label(label: str, option_id: str) -> str:
    normalized = (option_id or label).strip().lower().replace("_", " ")
    aliases = {
        "deny": "deny",
        "allow once": "allow once",
        "allow always same scope": "allow always",
        "reject with feedback": "reject: <feedback>",
    }
    return aliases.get(normalized, label.strip().lower() or option_id)
