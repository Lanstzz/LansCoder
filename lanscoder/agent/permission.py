"""权限领域协调器：mode / policy / sandbox / preflight / pending / review / bypass 一处归管。

会话、工具执行、子代理和权限恢复不再各自维护权限状态，只经 ``PermissionCoordinator``
交互。它拥有权限领域全套状态，并把 ``preflight -> pending 存储 -> bypass 评审`` 的
编排收拢成单一入口，避免协调器退化成 session 旧方法的薄透传。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from lanscoder.agent.prompt_inputs import DEFAULT_PERMISSION_POLICY
from lanscoder.agent.session import (
    AgentSession,
    PendingPermissionExecution,
    ToolPermissionPreflight,
)
from lanscoder.permissions.grants import PermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import (
    PermissionAction,
    PermissionDecisionKind,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    PermissionScopeType,
)
from lanscoder.providers.types import ToolCall
from lanscoder.runtime.user_input import UserInputRequest
from lanscoder.tools.permission_registry import PermissionAwareToolRegistry
from lanscoder.tools.permission_results import (
    make_permission_denied_result,
    make_prewrite_review_failed_result,
)
from lanscoder.tools.review import build_prewrite_review, supports_prewrite_review
from lanscoder.tools.types import ToolResult
from lanscoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


@dataclass(slots=True)
class PreparedPermission:
    """一次工具调用的权限准备结果（判别式数据类）。

    - ``result`` 非空：直接返回给执行层（DENY / prewrite review 失败）。
    - ``pending_input`` 非空：需要用户输入，本轮暂停（ASK / review-only）。
    - 两者皆空：已放行（ALLOW），执行层可继续；``prewrite_review`` 非空时
      由执行层把写前预览作为工具事件发出。
    """

    result: ToolResult | None = None
    pending_input: UserInputRequest | None = None
    permission_request: PermissionRequest | None = None
    prewrite_review: dict[str, object] | None = None


class PermissionCoordinator:
    """权限领域单一归口。

    四份状态（mode / permission_manager / sandbox_access / permission_policy）都在这里，
    一次 ``set_mode`` 同步全部；``prepare`` 编排 preflight / review / pending / bypass，
    供 ToolExecutor 直接消费。
    """

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
        """切换当前会话的权限策略模式，并同步 sandbox 与 policy。"""

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
        """对工具调用做权限预检，但不执行工具。

        只有权限 wrapper 支持这个能力；无权限声明的工具返回 ``None``，由旧执行路径
        直接处理。这样权限系统接入不会污染普通工具的执行模型。
        """

        registry = self._session.tool_registry
        if not isinstance(registry, PermissionAwareToolRegistry):
            return None
        preflight = registry.preflight(tool_call.name, tool_call.arguments)
        if preflight is None:
            return None
        _, _, request, decision = preflight
        return ToolPermissionPreflight(request=request, decision=decision)

    def requires_review(self, tool_call: ToolCall) -> bool:
        """非 BYPASS 模式下该工具是否需要在放行后仍暂停做写前预览。"""

        manager = self._permission_manager
        return self._session.require_prewrite_review and (manager is None or manager.mode != PermissionMode.BYPASS) and supports_prewrite_review(tool_call.name)

    def requires_bypass_review(self, tool_call: ToolCall) -> bool:
        """BYPASS 模式下该工具是否仍需计算写前预览（不暂停，失败即拒绝）。"""

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
        """resolve preflight 结果，判定是否需要暂停或 bypass 评审。"""

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
        """为权限确认暂停建立统一 pending 状态，返回 UI 确认请求或失败结果。"""

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
        # UI 会看到 confirmation.payload，但恢复时不信任 UI 回传的 tool_call。真实 tool_call
        # 保存在 session.pending_permission_execution 中，避免前端篡改参数后执行。
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
        """BYPASS 模式下对可写工具做写前预览；预览失败即拒绝，成功则放行。"""

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
        """为 ask_user 暂停建立统一 pending 状态。

        ask_user 工具本身不产生持久副作用，回答由 resume 阶段合成 tool_result 写入。
        这里把提问请求与同批次剩余工具一起保存，回答后继续执行剩余工具。
        """

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
        """按隔离模式产出一个 child 权限管理器（5c）。

        - inline（``root is None``）：克隆父权限管理器并加 ``NETWORK_REQUEST`` grant，
          保留父策略与模式。
        - isolated（``root`` 为 worktree，``mutation=True``）：自治 AGGRESSIVE 管理器，
          ``WRITE_PATH``/``DELETE_PATH`` 限定到 root。
        - background-inline（``root`` 为项目根，``mutation=False``）：自治 AGGRESSIVE
          管理器，无 mutation grant。
        """

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
