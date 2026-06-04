"""权限系统公共入口。

第一阶段只暴露纯策略和内存授权匹配，不直接绑定工具执行层。
"""

from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import (
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

__all__ = [
    "DefaultPermissionPolicy",
    "PermissionAction",
    "PermissionConfirmationChoice",
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionGrant",
    "PermissionGrantStore",
    "PermissionManager",
    "PermissionMode",
    "PermissionPersistence",
    "PermissionRequest",
    "PermissionScopeType",
]
