"""权限管理器:预检权限请求、构造确认/预写审查确认,并把用户选择解析为授权决定与长期授权。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from lanscoder.runtime.user_input import UserInputOption, UserInputRequest
from lanscoder.permissions.grants import PermissionGrantStore
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import (
    PermissionAction,
    PermissionConfirmationChoice,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionMode,
    PermissionPersistence,
    PermissionRequest,
    PermissionScopeType,
)


class PermissionManager:
    """权限管理器:结合授权存储与策略做预检,构造确认 UI,并把用户选择解析为决定。"""

    def __init__(
        self,
        *,
        policy: DefaultPermissionPolicy,
        grants: PermissionGrantStore | None = None,
        mode: PermissionMode = PermissionMode.STANDARD,
        autonomous: bool = False,
    ) -> None:
        """注入策略、授权存储与权限模式。"""
        self.policy = policy
        self.grants = grants or PermissionGrantStore()
        self.mode = mode
        self.autonomous = autonomous

    def preflight(self, request: PermissionRequest) -> PermissionDecision:
        """预检请求:授权命中则直接用,否则交策略决策;后台自动模式拒绝需确认的请求。"""
        request = self.normalize_request(request)
        grant_decision = self.grants.matching_decision(request)
        if grant_decision is not None:
            return grant_decision
        decision = self.policy.decide(request, mode=self.mode)
        if self.autonomous and decision.kind == PermissionDecisionKind.ASK:
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason=f"后台子 agent 无法交互确认，已自动拒绝：{decision.reason}",
            )
        return decision

    def build_confirmation(self, request: PermissionRequest) -> UserInputRequest:
        """构造权限确认的用户输入请求(含 deny/allow once/长期授权选项)。"""

        request = self.normalize_request(request)
        scope = default_scope_for_request(request, project_root=self.policy.project_root)
        question = _question_for_request(request)
        return UserInputRequest(
            id=request.id,
            kind="permission_confirmation",
            question=question,
            options=[
                UserInputOption(id=PermissionConfirmationChoice.DENY.value, label="Deny"),
                UserInputOption(id=PermissionConfirmationChoice.ALLOW_ONCE.value, label="Allow once"),
                *(
                    [
                        UserInputOption(
                            id=PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE.value,
                            label="Allow always",
                            description=f"{scope.scope_type.value}: {scope.scope_value}",
                        )
                    ]
                    if _allow_always_enabled(request)
                    else []
                ),
            ],
            payload={
                "request_type": "permission_confirmation",
                "permission_request_id": request.id,
                "action": request.action.value,
                "target": request.target,
                "reason": request.reason,
                "scope_type": scope.scope_type.value,
                "scope_value": scope.scope_value,
                "allow_always": _allow_always_enabled(request),
            },
        )

    def build_prewrite_review_confirmation(self, request: PermissionRequest) -> UserInputRequest:
        """构造预写审查确认(应用已预览的本地修改)。"""

        request = self.normalize_request(request)
        return UserInputRequest(
            id=request.id,
            kind="permission_confirmation",
            question=_question_for_request(request),
            options=[
                UserInputOption(id=PermissionConfirmationChoice.DENY.value, label="Deny"),
                UserInputOption(id=PermissionConfirmationChoice.ALLOW_ONCE.value, label="Apply reviewed change"),
            ],
            payload={
                "request_type": "prewrite_review_confirmation",
                "permission_request_id": request.id,
                "action": request.action.value,
                "target": request.target,
                "reason": "应用已预览的本地文件修改。",
                "allow_always": False,
            },
        )

    def resolve_confirmation(self, request: PermissionRequest, choice: str) -> PermissionDecision:
        """把用户选择解析为决定;允许时可选写入长期授权。"""

        request = self.normalize_request(request)
        normalized, feedback = _normalize_choice(choice)
        if normalized == PermissionConfirmationChoice.DENY:
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason="用户拒绝了权限请求。",
            )
        if normalized == PermissionConfirmationChoice.REJECT_WITH_FEEDBACK:
            if not feedback:
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="用户拒绝了权限请求。",
                )
            return PermissionDecision(
                kind=PermissionDecisionKind.DENY,
                reason=f"用户拒绝了权限请求：{feedback}",
                feedback=feedback,
            )
        if normalized == PermissionConfirmationChoice.ALLOW_ONCE:
            guard = self._confirmation_guard(request)
            if guard is not None:
                return guard
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                persistence=PermissionPersistence.ONCE,
                reason="用户允许本次执行。",
            )
        if normalized == PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE:
            if not _allow_always_enabled(request):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="该权限请求不支持长期授权。",
                )
            guard = self._confirmation_guard(request)
            if guard is not None:
                return guard
            scope = default_scope_for_request(request, project_root=self.policy.project_root)
            grant = PermissionGrant(
                id=f"grant_{request.id}",
                effect="allow",
                action=request.action,
                scope_type=scope.scope_type,
                scope_value=scope.scope_value,
                created_at=datetime.now(timezone.utc).isoformat(),
                reason="用户选择 allow always。",
            )
            self.grants.add(grant)
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                persistence=PermissionPersistence.ALWAYS,
                reason="用户允许同范围后续执行。",
                grant=grant,
            )
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"未知权限选择：{choice}",
        )

    def _confirmation_guard(self, request: PermissionRequest) -> PermissionDecision | None:
        """确认前的二次守卫:授权或策略已给出明确结论时直接返回。"""

        grant_decision = self.grants.matching_decision(request)
        if grant_decision is not None:
            return grant_decision

        policy_decision = self.policy.decide(request, mode=self.mode)
        if policy_decision.kind != PermissionDecisionKind.ASK:
            return policy_decision
        return None

    def normalize_request(self, request: PermissionRequest) -> PermissionRequest:
        """规范化请求:相对 cwd 解析为绝对,路径类请求补默认 cwd。"""

        if request.cwd is not None:
            if request.cwd.is_absolute():
                return request
            return replace(request, cwd=(self.policy.project_root / request.cwd).resolve())
        if request.action not in {
            PermissionAction.READ_PATH,
            PermissionAction.WRITE_PATH,
            PermissionAction.DELETE_PATH,
        }:
            return request
        return replace(request, cwd=self.policy.project_root)


class _PermissionScope:
    """权限范围(类型 + 值),用于长期授权的粒度。"""

    def __init__(self, *, scope_type: PermissionScopeType, scope_value: str) -> None:
        self.scope_type = scope_type
        self.scope_value = scope_value


def default_scope_for_request(request: PermissionRequest, *, project_root: Path | None = None) -> _PermissionScope:
    """按请求动作推导默认的权限范围。"""

    if request.action in {
        PermissionAction.READ_PATH,
        PermissionAction.WRITE_PATH,
        PermissionAction.DELETE_PATH,
    }:
        return _PermissionScope(
            scope_type=PermissionScopeType.EXACT_PATH,
            scope_value=_path_scope_value(request, project_root=project_root),
        )
    if request.action == PermissionAction.EXECUTE_SHELL:
        return _PermissionScope(
            scope_type=PermissionScopeType.COMMAND_PREFIX,
            scope_value=_shell_command_scope(request.target),
        )
    if request.action == PermissionAction.NETWORK_REQUEST:
        return _PermissionScope(scope_type=PermissionScopeType.HOST, scope_value=_host_scope_value(request.target))
    if request.action == PermissionAction.READ_ENV:
        return _PermissionScope(scope_type=PermissionScopeType.ENV_KEY, scope_value=request.target.upper())
    if request.action == PermissionAction.GIT_OPERATION:
        return _PermissionScope(scope_type=PermissionScopeType.COMMAND_PREFIX, scope_value=_git_command_scope(request.target))
    if request.action == PermissionAction.MCP_TOOL:
        return _PermissionScope(scope_type=PermissionScopeType.MCP_TOOL, scope_value=request.target)
    return _PermissionScope(scope_type=PermissionScopeType.COMMAND_PREFIX, scope_value=request.target.strip())


def _path_scope_value(request: PermissionRequest, *, project_root: Path | None) -> str:
    """把路径目标解析为绝对路径作为范围值。"""
    path = Path(request.target)
    if not path.is_absolute():
        path = (request.cwd or project_root or Path.cwd()) / path
    return str(path.resolve())


def _command_prefix(command: str) -> str:
    """提取命令前缀(git 命令取前两个词)。"""
    parts = command.strip().split()
    if not parts:
        return ""
    if len(parts) >= 2 and parts[0] == "git":
        return " ".join(parts[:2])
    return parts[0]


def _shell_command_scope(command: str) -> str:
    """shell 范围取整条命令。"""

    return command.strip()


def _git_command_scope(command: str) -> str:
    """git 范围取命令前缀。"""

    return _command_prefix(command)


def _host_scope_value(target: str) -> str:
    """从 URL 提取 host 作为网络请求的范围值。"""
    parsed = urlparse(target)
    host = parsed.hostname or target.split("/", 1)[0].split(":", 1)[0]
    return host.rstrip(".").lower()


def _question_for_request(request: PermissionRequest) -> str:
    """构造权限确认的问题文本。"""
    reason = f"\n原因：{request.reason}" if request.reason else ""
    return f"允许执行权限操作 `{request.action.value}` 吗？\n目标：{request.target}{reason}"


def _allow_always_enabled(request: PermissionRequest) -> bool:
    """请求元数据是否允许长期授权。"""
    return bool(request.metadata.get("allow_always", True))


def _normalize_choice(choice: str) -> tuple[PermissionConfirmationChoice | None, str]:
    """规范化用户选择文本,支持带反馈的拒绝。"""
    normalized = choice.strip().lower()
    for prefix in ("reject_with_feedback:", "reject:"):
        if normalized.startswith(prefix):
            feedback = choice.strip()[len(prefix) :].strip()
            return PermissionConfirmationChoice.REJECT_WITH_FEEDBACK, feedback
    numeric = {
        "1": PermissionConfirmationChoice.DENY,
        "2": PermissionConfirmationChoice.ALLOW_ONCE,
        "3": PermissionConfirmationChoice.ALLOW_ALWAYS_SAME_SCOPE,
        "4": PermissionConfirmationChoice.REJECT_WITH_FEEDBACK,
    }
    if normalized in numeric:
        return numeric[normalized], ""
    for item in PermissionConfirmationChoice:
        if normalized == item.value:
            return item, ""
    return None, ""
