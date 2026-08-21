from dataclasses import asdict

from lanscoder.permissions.types import PermissionDecision, PermissionRequest
from lanscoder.permissions.user_input import UserInputOption, UserInputRequest
from lanscoder.providers.types import ToolCall
from lanscoder.tools.types import ToolResult, make_error_result, make_text_result


def user_input_request_from_tool_result(
    result: ToolResult,
    *,
    tool_call_id: str,
    tool_name: str,
) -> UserInputRequest | None:

    data = getattr(result, "data", {}) or {}
    if not data.get("requires_user_input"):
        return None

    request_type = str(data.get("request_type") or "ask_user")
    if request_type not in {"ask_user", "permission_confirmation"}:
        request_type = "ask_user"

    content = str(getattr(result, "content", "") or "")
    question = str(data.get("question") or content).strip()
    if not question:
        question = "需要用户输入。"

    options = options_from_data(data.get("options"))
    request_id = str(data.get("request_id") or data.get("permission_request_id") or tool_call_id)
    payload = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_result_name": getattr(result, "name", tool_name),
    }
    for key in ("request_type", "permission_request_id", "permission_request", "pending_tool_call"):
        if key in data:
            payload[key] = data[key]

    return UserInputRequest(
        id=request_id,
        kind=request_type,  # type: ignore[arg-type]
        question=question,
        options=options,
        payload=payload,
    )


def options_from_data(raw_options: object) -> list[UserInputOption]:
    if not isinstance(raw_options, list):
        return []

    options: list[UserInputOption] = []
    for index, raw_option in enumerate(raw_options, start=1):
        if isinstance(raw_option, dict):
            label = str(raw_option.get("label") or raw_option.get("id") or "").strip()
            if not label:
                continue
            option_id = str(raw_option.get("id") or index)
            description = str(raw_option.get("description") or "")
        else:
            label = str(raw_option).strip()
            if not label:
                continue
            option_id = str(index)
            description = ""
        options.append(UserInputOption(id=option_id, label=label, description=description))
    return options


def make_permission_confirmation_result(
    *,
    tool_name: str,
    request: PermissionRequest,
    confirmation: UserInputRequest,
    pending_tool_call: ToolCall | None = None,
) -> ToolResult:

    data = {
        "requires_user_input": True,
        "request_type": "permission_confirmation",
        "permission_request_id": request.id,
        "question": confirmation.question,
        "options": [asdict(option) for option in confirmation.options],
        "permission_request": _permission_request_data(request),
    }
    if pending_tool_call is not None:
        data["pending_tool_call"] = {
            "id": pending_tool_call.id,
            "name": pending_tool_call.name,
            "arguments": pending_tool_call.arguments,
        }
    return make_text_result(tool_name, confirmation.question, **data)


def make_permission_denied_result(
    *,
    tool_name: str,
    request: PermissionRequest,
    decision: PermissionDecision,
) -> ToolResult:

    data = {
        "request_type": "permission_denied",
        "permission_request_id": request.id,
        "permission_decision": decision.kind.value,
        "permission_request": _permission_request_data(request),
    }
    if decision.feedback:
        data["permission_feedback"] = decision.feedback
    return make_error_result(
        tool_name,
        decision.reason or "权限请求被拒绝。",
        **data,
    )


def make_prewrite_review_stale_result(*, tool_name: str, request: PermissionRequest) -> ToolResult:

    return make_error_result(
        tool_name,
        "写前预览已过期：文件在确认前发生变化，请重新生成 diff 后再确认。",
        request_type="prewrite_review_stale",
        permission_request_id=request.id,
        permission_request=_permission_request_data(request),
    )


def make_prewrite_review_failed_result(
    *,
    tool_name: str,
    request: PermissionRequest,
    error: str,
) -> ToolResult:

    return make_error_result(
        tool_name,
        f"无法生成写前预览：{error}",
        request_type="prewrite_review_failed",
        permission_request_id=request.id,
        permission_request=_permission_request_data(request),
    )


def _permission_request_data(request: PermissionRequest) -> dict[str, object]:
    return {
        "id": request.id,
        "action": request.action.value,
        "target": request.target,
        "reason": request.reason,
        "cwd": str(request.cwd) if request.cwd is not None else None,
        "metadata": request.metadata,
    }
