from __future__ import annotations


def permission_choice_for_text(text: str, pending) -> str | None:
    """把用户输入解析为权限选择:只接受数字 1..N(按选项顺序)。

    prewrite review 场景额外接受 `reject: <反馈>` 以驳回并附理由;
    其余输入一律视为无效,由调用方提示只能输入有效编号。
    """
    raw = text.strip()
    normalized = raw.lower()
    payload = getattr(pending, "payload", {}) or {}
    if isinstance(payload.get("prewrite_review"), dict) and normalized.startswith(("reject:", "reject_with_feedback:")):
        return f"reject_with_feedback: {raw.split(':', 1)[1].strip()}"
    if not raw.isdigit():
        return None
    index = int(raw) - 1
    options = list(getattr(pending, "options", []) or [])
    if index < 0 or index >= len(options):
        return None
    return str(getattr(options[index], "id", "") or "")


def permission_options_text(pending) -> str:
    options = getattr(pending, "options", []) or []
    if not options:
        return "只能输入许可编号(1/2/3)确认；其他输入无效。"
    choices: list[str] = []
    for index, option in enumerate(options, start=1):
        label = str(getattr(option, "label", "") or getattr(option, "id", "") or "")
        option_id = str(getattr(option, "id", "") or "")
        choices.append(f"[{index}] {permission_option_label(label, option_id)}")
    allowed = "/".join(map(str, range(1, len(options) + 1)))
    hint = f"只能输入 {allowed}：{'  '.join(choices)}"
    payload = getattr(pending, "payload", {}) or {}
    if isinstance(payload.get("prewrite_review"), dict):
        hint += "；写前审查可输入 reject: <反馈>"
    return hint


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


def ask_user_prompt_text(pending) -> str:
    question = str(getattr(pending, "question", "") or "需要用户输入。")
    lines = [question]
    options = list(getattr(pending, "options", []) or [])
    if options:
        choices = [f"[{index}] {str(getattr(option, 'label', '') or getattr(option, 'id', '') or '')}" for index, option in enumerate(options, start=1)]
        lines.append("  " + "  ".join(choices))
    return "\n".join(lines)


def ask_user_choice_for_text(text: str, pending) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    for index, option in enumerate(getattr(pending, "options", []) or [], start=1):
        option_id = str(getattr(option, "id", "") or "")
        label = str(getattr(option, "label", "") or option_id or "")
        values = {
            str(index),
            option_id.lower(),
            label.strip().lower().replace(" ", "_"),
        }
        if normalized in values:
            return label
    return None
