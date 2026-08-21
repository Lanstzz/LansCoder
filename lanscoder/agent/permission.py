from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from lanscoder.agent.permission_results import (
    make_permission_denied_result,
    make_prewrite_review_failed_result,
)
from lanscoder.agent.prompt_inputs import DEFAULT_PERMISSION_POLICY
from lanscoder.agent.session import (
    AgentSession,
    PendingPermissionExecution,
    ToolPermissionPreflight,
)
from lanscoder.permissions.classification import ClassificationSpec, build_request, classify
from lanscoder.permissions.grants import PermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import (
    PermissionAction,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    PermissionScopeType,
)
from lanscoder.providers.types import ToolCall
from lanscoder.permissions.user_input import UserInputRequest
from lanscoder.tools.review import build_prewrite_review, supports_prewrite_review
from lanscoder.tools.types import ToolResult
from lanscoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


@dataclass(slots=True)
class PreparedPermission:

    result: ToolResult | None = None
    pending_input: UserInputRequest | None = None
    permission_request: PermissionRequest | None = None
    prewrite_review: dict[str, object] | None = None


def _deny_invalid(
    tool_name: str,
    arguments: dict,
    spec: ClassificationSpec,
    exc: ValueError,
) -> ToolPermissionPreflight:
    """参数缺失短路:固定 id=f"perm_{name}_invalid",reason 为原 ValueError 文案,不调 manager.preflight。"""
    request = PermissionRequest(
        id=f"perm_{tool_name}_invalid",
        action=spec.action,
        target="",
        reason=str(exc),
        metadata={"tool_name": tool_name, "arguments": dict(arguments)},
    )
    decision = PermissionDecision(kind=PermissionDecisionKind.DENY, reason=str(exc))
    return ToolPermissionPreflight(request=request, decision=decision)


class PermissionCoordinator:

    def __init__(
        self,
        *,
        session: AgentSession,
        permission_manager: PermissionManager | None,
        sandbox_access: SandboxAccess,
    ) -> None:
        self._session = session
        self._permission_manager = permission_manager
        self._sandbox_access = sandbox_access
        self._mode = permission_manager.mode if permission_manager is not None else PermissionMode.STANDARD
        self._permission_policy = dict(DEFAULT_PERMISSION_POLICY)
        self._last_review_payload: dict[str, object] | None = None
        self._sync_sandbox_access_with_mode()

    def set_mode(self, mode: PermissionMode | str) -> PermissionMode:

        resolved = PermissionMode(str(mode))
        self._mode = resolved
        if self._permission_manager is not None:
            self._permission_manager.mode = resolved
        self._sync_sandbox_access_with_mode()
        return resolved

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    @property
    def permission_manager(self) -> PermissionManager | None:
        return self._permission_manager

    @property
    def sandbox_access(self) -> SandboxAccess:
        return self._sandbox_access

    @property
    def permission_policy(self) -> dict[str, object]:
        return self._permission_policy

    def _sync_sandbox_access_with_mode(self) -> None:
        if self._mode == PermissionMode.BYPASS:
            self._sandbox_access.mode = SandboxAccessMode.UNRESTRICTED
            self._permission_policy["path_access"] = "unrestricted"
            self._permission_policy["read"] = "allow"
            self._permission_policy["write"] = "allow"
            self._permission_policy["delete"] = "allow"
            self._permission_policy["shell"] = "allow"
            self._permission_policy["network"] = "allow"
            return

        self._sandbox_access.mode = SandboxAccessMode.PROJECT
        self._permission_policy["path_access"] = DEFAULT_PERMISSION_POLICY["path_access"]
        self._permission_policy["read"] = DEFAULT_PERMISSION_POLICY["read"]
        self._permission_policy["write"] = DEFAULT_PERMISSION_POLICY["write"]
        self._permission_policy["delete"] = DEFAULT_PERMISSION_POLICY["delete"]
        self._permission_policy["shell"] = DEFAULT_PERMISSION_POLICY["shell"]
        self._permission_policy["network"] = DEFAULT_PERMISSION_POLICY["network"]

    def preflight(self, tool_call: ToolCall) -> ToolPermissionPreflight | None:

        manager = self._permission_manager
        if manager is None:
            return None
        arguments = tool_call.arguments
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return None
        spec = classify(tool_call.name, arguments)
        if spec is None:
            return None
        try:
            request = build_request(tool_call.name, arguments)
        except ValueError as exc:
            return _deny_invalid(tool_call.name, arguments, spec, exc)
        request = manager.normalize_request(request)
        decision = manager.preflight(request)
        return ToolPermissionPreflight(request=request, decision=decision)

    def requires_review(self, tool_call: ToolCall) -> bool:

        manager = self._permission_manager
        return self._session.require_prewrite_review and (manager is None or manager.mode != PermissionMode.BYPASS) and supports_prewrite_review(tool_call.name)

    def requires_bypass_review(self, tool_call: ToolCall) -> bool:

        manager = self._permission_manager
        return (
            self._session.require_prewrite_review
            and manager is not None
            and manager.mode == PermissionMode.BYPASS
            and self._session.tool_registry.get(tool_call.name) is not None
            and supports_prewrite_review(tool_call.name)
        )

    def prepare(
        self,
        tool_call: ToolCall,
        deferred_tool_calls: list[ToolCall],
    ) -> PreparedPermission:

        preflight = self.preflight(tool_call)
        if preflight is None:
            return PreparedPermission()
        if preflight.decision.kind == PermissionDecisionKind.DENY:
            return PreparedPermission(
                result=make_permission_denied_result(
                    tool_name=tool_call.name,
                    request=preflight.request,
                    decision=preflight.decision,
                ),
                permission_request=preflight.request,
            )
        review_only = preflight.decision.kind == PermissionDecisionKind.ALLOW and self.requires_review(tool_call)
        if preflight.decision.kind == PermissionDecisionKind.ASK or review_only:
            pending = self.store_pending_request(
                tool_call=tool_call,
                request=preflight.request,
                deferred_tool_calls=deferred_tool_calls,
                review_only=review_only,
            )
            if isinstance(pending, ToolResult):
                return PreparedPermission(
                    result=pending,
                    permission_request=preflight.request,
                )
            return PreparedPermission(
                pending_input=pending,
                permission_request=preflight.request,
            )
        result = self.bypass_mutation(tool_call, preflight=preflight)
        return PreparedPermission(
            result=result,
            permission_request=preflight.request,
            prewrite_review=None if result is not None else self._last_review_payload,
        )

    def store_pending_request(
        self,
        *,
        tool_call: ToolCall,
        request: PermissionRequest,
        deferred_tool_calls: list[ToolCall],
        review_only: bool = False,
    ) -> UserInputRequest | ToolResult:

        if self._permission_manager is None:
            raise RuntimeError("permission confirmation requires a permission manager")

        confirmation = self._permission_manager.build_prewrite_review_confirmation(request) if review_only else self._permission_manager.build_confirmation(request)
        prewrite_review = None
        if supports_prewrite_review(tool_call.name):
            prewrite_review = build_prewrite_review(
                self._permission_manager.policy.project_root,
                tool_call,
                access=self._sandbox_access,
            )
            if not prewrite_review.ok:
                return make_prewrite_review_failed_result(
                    tool_name=tool_call.name,
                    request=request,
                    error=prewrite_review.error or "未知错误",
                )
            confirmation.payload["prewrite_review"] = prewrite_review.to_payload()
        trusted_tool_call = ToolCall(
            id=tool_call.id,
            name=tool_call.name,
            arguments=deepcopy(tool_call.arguments),
        )
        confirmation.payload["pending_tool_call"] = {
            "id": trusted_tool_call.id,
            "name": trusted_tool_call.name,
            "arguments": deepcopy(trusted_tool_call.arguments),
        }
        self._session.pending_permission_execution = PendingPermissionExecution(
            request_id=request.id,
            tool_call=trusted_tool_call,
            permission_request=request,
            prewrite_review=prewrite_review,
            review_only=review_only,
            deferred_tool_calls=list(deferred_tool_calls),
        )
        self._session.persist_pending_permission_kind(
            tool_call_id=trusted_tool_call.id,
            review_only=review_only,
        )
        return confirmation

    def bypass_mutation(
        self,
        tool_call: ToolCall,
        *,
        preflight,
    ) -> ToolResult | None:

        self._last_review_payload = None
        if not self.requires_bypass_review(tool_call):
            return None
        if preflight is None:
            return None
        review = build_prewrite_review(
            self._permission_manager.policy.project_root,
            tool_call,
            access=self._sandbox_access,
        )
        if not review.ok:
            return make_prewrite_review_failed_result(
                tool_name=tool_call.name,
                request=preflight.request,
                error=review.error or "未知错误",
            )
        self._last_review_payload = review.to_payload()
        return None

    def store_pending_ask_user(
        self,
        *,
        tool_call: ToolCall,
        deferred_tool_calls: list[ToolCall],
        user_input_request: UserInputRequest,
    ) -> None:

        trusted_tool_call = ToolCall(
            id=tool_call.id,
            name=tool_call.name,
            arguments=deepcopy(tool_call.arguments),
        )
        self._session.pending_permission_execution = PendingPermissionExecution(
            request_id=user_input_request.id,
            tool_call=trusted_tool_call,
            kind="ask_user",
            deferred_tool_calls=list(deferred_tool_calls),
            ask_user_request=user_input_request,
        )

    def pending_get(self, request_id: str) -> PendingPermissionExecution | None:
        pending = self._session.pending_permission_execution
        if pending is not None and pending.request_id == request_id:
            return pending
        return None

    def pending_clear(self) -> None:
        self._session.pending_permission_execution = None

    def child_permission_manager(
        self,
        *,
        root: str | None,
        mutation: bool,
        background: bool,
    ) -> PermissionManager | None:

        if root is None:
            return self._child_permission_manager_for_inline()
        return self._build_autonomous_child_permission_manager(root, mutation=mutation)

    def _child_permission_manager_for_inline(self) -> PermissionManager | None:
        if self._permission_manager is None:
            return None
        grants = PermissionGrantStore(grants=self._permission_manager.grants.list())
        grants.add(
            PermissionGrant(
                id="grant_subagent_network_request",
                effect="allow",
                action=PermissionAction.NETWORK_REQUEST,
                scope_type=PermissionScopeType.HOST,
                scope_value="*",
                created_at="runtime",
                reason="Subagent may make read-only network requests (web_search, fetch).",
            )
        )
        return PermissionManager(
            policy=self._permission_manager.policy,
            grants=grants,
            mode=self._permission_manager.mode,
        )

    def _build_autonomous_child_permission_manager(
        self,
        root: str,
        *,
        mutation: bool,
    ) -> PermissionManager:
        grants = PermissionGrantStore()
        if mutation:
            root_value = str(root)
            for action in (PermissionAction.WRITE_PATH, PermissionAction.DELETE_PATH):
                grants.add(
                    PermissionGrant(
                        id=f"grant_subagent_{action.value}",
                        effect="allow",
                        action=action,
                        scope_type=PermissionScopeType.PATH_TREE,
                        scope_value=root_value,
                        created_at="runtime",
                        reason="Isolated background coder may mutate only its dedicated worktree.",
                    )
                )
        grants.add(
            PermissionGrant(
                id="grant_subagent_network_request",
                effect="allow",
                action=PermissionAction.NETWORK_REQUEST,
                scope_type=PermissionScopeType.HOST,
                scope_value="*",
                created_at="runtime",
                reason="Subagent may make read-only network requests (web_search, fetch).",
            )
        )
        return PermissionManager(
            policy=DefaultPermissionPolicy(root),
            grants=grants,
            mode=PermissionMode.AGGRESSIVE,
            autonomous=True,
        )
