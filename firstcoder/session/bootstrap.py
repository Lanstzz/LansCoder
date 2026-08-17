"""Shared AgentSession assembly for new / resume / fork / factory paths."""

# ============================================================================
# 阅读路径导航 (Reading Path Guide)
# ============================================================================
# SessionBootstrap 是创建/恢复 AgentSession 的唯一合法路径 (single assembly path)。
# 架构规则：所有 session 组装（新建、恢复、fork、factory）都必须经过这里。
# 如果你发现绕过 SessionBootstrap 的组装路径，那是技术债 (debt)，不是模板。
#
# Bootstrap 负责把以下组件统一注入 AgentSession：
#   - JsonlSessionStore  (持久化存储)
#   - agents_md          (项目级指令)
#   - skill_catalog      (技能目录)
#   - tools              (工具列表)
#   - permission_manager (权限管理器 + grants)
#   - sandbox_access     (沙箱访问控制)
#
# → 来自：firstcoder/app/factory.py (create_firstcoder_app 中创建 SessionBootstrap)
# → 下游：firstcoder/agent/session.py (AgentSession.create / .resume / .from_project)
# ============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firstcoder.agent.prompt_inputs import read_agents_md
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.context.identity import new_session_id
from firstcoder.context.store import JsonlSessionStore
from firstcoder.memory.manager import MemoryManager, project_memory_root
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


# ----------------------------------------------------------------------------
# SessionBootstrap 字段说明：
#   - store:          JsonlSessionStore，JSONL 格式的 session 持久化存储
#                     (来自 firstcoder/context/store.py)
#   - project_root:   项目根目录，用于定位 AGENTS.md 并作为权限策略的作用域
#   - data_root:      数据根目录（permissions.json 等状态文件存放处）；
#                     未指定时回退到 store.root
#   - tools:          静态工具列表 (list[Tool])，直接注入到 AgentSession
#   - tools_provider: 动态工具提供器 (Callable[[], list[Tool]])，延迟调用以支持
#                     运行时变化的工具集合；与 tools 二选一，provider 优先
#   - sandbox_access: 沙箱访问控制，决定是否允许越出项目根的文件/命令操作
# ----------------------------------------------------------------------------
@dataclass(slots=True)
class SessionBootstrap:
    """Single place that knows how to build a project-bound AgentSession."""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    user_memory_root: str | Path | None = None

    def resolved_data_root(self) -> Path:
        return Path(self.data_root) if self.data_root is not None else self.store.root

    def resolve_tools(self) -> list[Tool] | None:
        return self.tools_provider() if self.tools_provider is not None else self.tools

    # ----------------------------------------------------------------------------
    # 构造 PermissionManager：
    #   - create_project_permission_manager 来自 firstcoder/agent/session.py，
    #     它按 project_root 建立项目级的权限策略（哪些路径可写/可执行等）
    #   - FilePermissionGrantStore 来自 firstcoder/permissions/grants.py，
    #     以 JSON 文件形式持久化用户的 allow/deny grants
    #   - grants 文件路径：{data_root}/permissions.json
    # ----------------------------------------------------------------------------
    def permission_manager(self) -> PermissionManager:
        return create_project_permission_manager(
            self.project_root,
            grants=FilePermissionGrantStore(self.resolved_data_root() / "permissions.json"),
        )

    def memory_manager(self) -> MemoryManager:
        user_root = (
            Path(self.user_memory_root)
            if self.user_memory_root is not None
            else Path.home() / ".firstcoder" / "memory"
        )
        return MemoryManager(
            user_root=user_root,
            project_root=project_memory_root(self.resolved_data_root(), Path(self.project_root)),
        )

    # ----------------------------------------------------------------------------
    # 新建 session 路径：
    #   - AgentSession.create 来自 firstcoder/agent/session.py
    #   - 读取项目根目录下的 AGENTS.md（read_agents_md 来自
    #     firstcoder/agent/prompt_inputs.py），作为 system prompt 的项目级指令
    #   - 发现项目可用 skills（discover_all_skills 来自
    #     firstcoder/skills/discovery.py），构建 skill_catalog
    #   - 若未指定 session_id，则由 new_session_id() 生成
    #     (firstcoder/context/identity.py)
    # ----------------------------------------------------------------------------
    def create(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession.create(
            store=self.store,
            session_id=session_id or new_session_id(),
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )

    # ----------------------------------------------------------------------------
    # 恢复 session 路径：
    #   - AgentSession.resume 来自 firstcoder/agent/session.py
    #   - 与 create() 使用相同的注入参数（agents_md / skill_catalog / tools /
    #     permission_manager / sandbox_access），但通过 session_id 从 JSONL store
    #     重建 runtime state（历史消息、工具调用状态等）
    #   - session_id 必须由调用方提供，不能为空
    # ----------------------------------------------------------------------------
    def resume(self, session_id: str) -> AgentSession:
        return AgentSession.resume(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )

    # ----------------------------------------------------------------------------
    # 从项目创建 session 路径（factory.py 默认使用此入口）：
    #   - AgentSession.from_project 来自 firstcoder/agent/session.py
    #   - 注意：这里不传 agents_md 和 skill_catalog —— AgentSession.from_project
    #     内部会基于 project_root 自行读取/发现，避免在 bootstrap 层重复 IO
    #   - 这是 create_firstcoder_app (firstcoder/app/factory.py) 启动 app 时
    #     默认走的 session 组装路径
    # ----------------------------------------------------------------------------
    def from_project(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession.from_project(
            store=self.store,
            session_id=session_id or new_session_id(),
            project_root=self.project_root,
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
            memory_manager=self.memory_manager(),
        )
