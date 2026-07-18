"""权限系统专用 ToolResult helper。"""

from __future__ import annotations

from dataclasses import asdict

from firstcoder.runtime.user_input import UserInputRequest
from firstcoder.permissions.types import PermissionDecision, PermissionRequest
from firstcoder.providers.types import ToolCall
from firstcoder.tools.types import ToolResult, make_error_result, make_text_result


def make_permission_confirmation_result(
    *,
    tool_name: str,
    request: PermissionRequest,
    confirmation: UserInputRequest,
    pending_tool_call: ToolCall | None = None,
) -> ToolResult:
    """创建会让 agent loop 暂停的权限确认结果。"""

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
    """创建统一的权限拒绝结果。"""

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
    """Block an approved mutation when the reviewed filesystem snapshot changed."""

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
    """Reject a direct mutation whose trusted preview cannot be produced."""

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
