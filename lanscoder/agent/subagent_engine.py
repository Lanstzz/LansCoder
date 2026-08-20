"""Subagent engine for the delegate tool.

The child runner is not constructed here: the assembly root injects a
``child_runner_factory`` so this module never imports ``AgentLoop`` (breaking
the historical ``agent.loop -> agent.subagent -> agent.loop`` import cycle).
The engine keeps the boundary deliberately small:

- child sessions are fresh and metadata-tagged;
- tool access is profile restricted;
- children do not receive the delegate tool, preventing recursion;
- foreground execution returns only a compact summary to the parent;
- background execution is handled by the Phase 1 generic async runtime.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lanscoder.agent.background import BackgroundJobManager, current_job_id
from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.agent.observer import TurnObserver
from lanscoder.agent.ports import SessionTurnRunner
from lanscoder.agent.session import AgentSession
from lanscoder.agent.worktree import (
    Worktree,
    WorktreeDiff,
    WorktreeError,
    WorktreeManager,
)
from lanscoder.context.identity import new_session_id
from lanscoder.context.store import JsonlSessionStore
from lanscoder.permissions.grants import PermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import (
    PermissionAction,
    PermissionGrant,
    PermissionMode,
    PermissionScopeType,
)
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import MainRequestOptions
from lanscoder.runtime.cancellation import (
    AgentCancelledError,
    current_cancellation_token,
)
from lanscoder.skills.models import SkillCatalog
from lanscoder.subagent.types import (
    SUBAGENT_PROFILES,
    SubagentProfile,
    SubagentRequest,
    SubagentResult,
)
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode

logger = logging.getLogger(__name__)


class SubagentEngine:
    """Create and run isolated child sessions for the delegate tool.

    The concrete child loop is produced by ``child_runner_factory`` (a
    ``Callable[..., SessionTurnRunner]`` injected by the assembly root), so this
    class never imports ``AgentLoop`` and the layer stays cycle-free.
    """

    def __init__(
        self,
        *,
        store: JsonlSessionStore,
        provider: ChatProvider,
        tools: list[Tool],
        project_root: str | Path | None = None,
        agents_md: str = "",
        skill_catalog: SkillCatalog | None = None,
        permission_manager: PermissionManager | None = None,
        sandbox_access: SandboxAccess | None = None,
        request_options: MainRequestOptions | None = None,
        limits: AgentLoopLimits | None = None,
        background_manager: BackgroundJobManager | None = None,
        child_runner_factory: Callable[..., SessionTurnRunner],
    ) -> None:
        self.store = store
        self.provider = provider
        self.tools = list(tools)
        self.project_root = Path(project_root).resolve() if project_root is not None else None
        self.agents_md = agents_md
        self.skill_catalog = skill_catalog or SkillCatalog()
        self.permission_manager = permission_manager
        self.sandbox_access = sandbox_access or SandboxAccess()
        self.request_options = request_options or MainRequestOptions()
        self.limits = limits or AgentLoopLimits(max_tool_rounds=20, max_provider_calls=40, max_turn_seconds=600)
        self.background_manager = background_manager
        self.child_runner_factory = child_runner_factory
        # 前台 delegate 运行时的实时进度，TUI 用它渲染输入栏下方的 activity 行；
        # 无前台 delegate 时为 None。
        self.foreground_progress: dict[str, Any] | None = None

    def profile(self, role: str) -> SubagentProfile | None:
        return SUBAGENT_PROFILES.get(str(role))

    def tools_for_role(self, role: str) -> list[Tool]:
        profile = self.profile(role)
        if profile is None:
            return []
        return [tool for tool in self.tools if tool.name in profile.allowed_tool_names and tool.name != "delegate"]

    def run(self, request: SubagentRequest) -> SubagentResult:
        profile = self.profile(request.role)
        if profile is None:
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id="",
                summary=f"Unknown subagent role: {request.role}",
                error="unknown_role",
            )
        if request.run_in_background and not profile.allow_background:
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id="",
                summary=f"{request.role} 不支持后台执行。",
                error="background_not_allowed",
            )

        # 进度去向是每次 run 独立的决定：前台（主线程、无后台 job）建一个 tracker 挂到
        # self.foreground_progress 供 TUI 读取，后台委托由 job.progress 承载。tracker 作为
        # 本次 run 的局部量贯穿整个 run —— 后台 run 的 tracker 恒为 None，因此永远清不掉
        # 前台正在用的 tracker（曾有的竞态：后台 run 的 finally 无条件清掉前台进度而崩溃）。
        tracker: dict[str, Any] | None = None
        if not request.run_in_background and current_job_id() is None:
            tracker = {
                "label": request.role,
                "started_at": time.monotonic(),
                "provider_calls": 0,
                "total_tokens": 0,
            }
            self.foreground_progress = tracker
        try:
            if self._needs_worktree(request, profile=profile):
                return self._run_isolated(request, profile=profile, progress_tracker=tracker)
            return self._run_inline(request, profile=profile, progress_tracker=tracker)
        finally:
            if tracker is not None:
                self.foreground_progress = None

    def _needs_worktree(self, request: SubagentRequest, *, profile: SubagentProfile) -> bool:
        """Whether this run must execute inside an isolated git worktree.

        Mutation-capable roles isolate when running in the background so a
        background job can never touch the parent working tree.  Callers can also
        force isolation explicitly via ``request.isolate_worktree`` (used by the
        parent ToolExecutor when it backgrounds a coder delegate).
        """

        if request.isolate_worktree:
            return True
        return bool(profile.requires_worktree and request.run_in_background)

    def _run_inline(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        progress_tracker: dict[str, Any] | None,
    ) -> SubagentResult:
        """Original Phase 2 behaviour: run the child against the parent-rooted tools."""

        child_session = self.create_child_session(request, profile=profile)
        started = time.monotonic()
        try:
            prompt = self._child_prompt(request, profile=profile)
            result, runner = self._run_child_loop(
                child_session,
                prompt,
                self.tools_for_role(request.role),
                self._make_child_observer(progress_tracker),
            )
            usage = runner.usage_summary()
            response = result.response if result is not None else None
            if response is not None:
                if response.finish_reason == "interrupted":
                    raise AgentCancelledError()
                content = response.content.strip() or "Subagent finished without text output."
                return SubagentResult(
                    ok=True,
                    role=request.role,
                    child_session_id=child_session.session_id,
                    summary=content,
                    total_tokens=usage["total_tokens"],
                    provider_calls=usage["provider_calls"],
                    elapsed_seconds=time.monotonic() - started,
                )
            summary, error = ("subagent paused for user input", "subagent paused for user input") if result is not None else ("subagent failed without a result", "child_loop_failed")
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id=child_session.session_id,
                summary=summary,
                error=error,
                total_tokens=usage["total_tokens"],
                provider_calls=usage["provider_calls"],
                elapsed_seconds=time.monotonic() - started,
            )
        finally:
            self._delete_child_session(child_session.session_id)

    def _run_isolated(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        progress_tracker: dict[str, Any] | None,
    ) -> SubagentResult:
        """Phase 4: run a mutation-capable child inside a dedicated git worktree.

        The child gets fresh tools rooted at the worktree and a child
        ``PermissionManager`` whose ``project_root`` is the worktree path, so it
        can never read or mutate the parent working tree.  On completion the
        worktree is left in place and a diff summary is returned for explicit
        parent review; nothing is auto-merged.
        """

        if self.project_root is None:
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id="",
                summary="无法隔离执行：未知项目根目录。",
                error="worktree_unavailable",
            )
        manager = WorktreeManager(self.project_root)
        if not manager.available():
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id="",
                summary="无法隔离执行：当前项目不是 git 仓库，后台 coder 需要 worktree 隔离。",
                error="worktree_unavailable",
            )

        session_id = new_session_id()
        try:
            worktree = manager.create(session_id)
        except WorktreeError as exc:
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id=session_id,
                summary=f"创建隔离 worktree 失败：{exc}",
                error="worktree_create_failed",
            )
        self._attach_worktree_cleanup(manager, worktree)

        try:
            child_session = self._create_isolated_child_session(request, profile=profile, worktree=worktree, session_id=session_id)
            try:
                prompt = self._child_prompt(request, profile=profile, worktree=worktree)
                started = time.monotonic()
                result, runner = self._run_child_loop(
                    child_session,
                    prompt,
                    self._worktree_child_tools(
                        worktree.path,
                        profile=profile,
                        access=child_session.sandbox_access,
                    ),
                    self._make_child_observer(progress_tracker),
                )
                usage = runner.usage_summary()
                diff = manager.diff(worktree)
                response = result.response if result is not None else None
                if response is not None:
                    if response.finish_reason == "interrupted":
                        raise AgentCancelledError()
                    content = response.content.strip() or "Subagent finished without text output."
                    summary = self._compose_isolated_summary(content, worktree=worktree, diff=diff)
                    return SubagentResult(
                        ok=True,
                        role=request.role,
                        child_session_id=session_id,
                        summary=summary,
                        files_changed=diff.files_changed,
                        worktree_path=str(worktree.path),
                        worktree_branch=worktree.branch,
                        diff_summary=diff.render(),
                        total_tokens=usage["total_tokens"],
                        provider_calls=usage["provider_calls"],
                        elapsed_seconds=time.monotonic() - started,
                    )
                summary, error = (
                    ("隔离 coder 等待用户输入，无法在后台继续。", "waiting_for_user_input") if result is not None else ("隔离 coder 执行失败：child loop 未产出结果。", "child_loop_failed")
                )
                return SubagentResult(
                    ok=False,
                    role=request.role,
                    child_session_id=session_id,
                    summary=summary,
                    error=error,
                    files_changed=diff.files_changed,
                    worktree_path=str(worktree.path),
                    worktree_branch=worktree.branch,
                    diff_summary=diff.render(),
                    total_tokens=usage["total_tokens"],
                    provider_calls=usage["provider_calls"],
                    elapsed_seconds=time.monotonic() - started,
                )
            finally:
                self._delete_child_session(session_id)
        except AgentCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - defensive: setup failures must not break parent loop
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id=session_id,
                summary=f"隔离执行初始化失败：{exc}",
                error=str(exc),
                worktree_path=str(worktree.path),
                worktree_branch=worktree.branch,
            )

    def create_child_session(self, request: SubagentRequest, *, profile: SubagentProfile) -> AgentSession:
        session_id = new_session_id()
        permission_manager = self._background_child_permission_manager() if request.run_in_background else self._child_permission_manager_for_inline()
        child = AgentSession.create(
            store=self.store,
            session_id=session_id,
            agents_md=self.agents_md,
            skill_catalog=self.skill_catalog,
            tools=self._supplied_tools_for_child(profile.role),
            permission_manager=permission_manager,
            sandbox_access=self.sandbox_access,
        )
        child.writer.append_session_metadata_updated(
            parent_session_id=request.parent_session_id,
            delegate_role=profile.role,
            delegate_task=request.task,
        )
        return child

    def _delete_child_session(self, session_id: str) -> None:
        """Best-effort cleanup of a finished child session.

        Child sessions are ephemeral working transcripts; once the subagent has
        produced its result they must not linger in ``/resume``. Cleanup must
        never break the parent loop, so failures are swallowed.
        """
        try:
            self.store.delete_session(session_id)
        except Exception:  # noqa: BLE001 - cleanup must never break the parent loop
            pass

    def _supplied_tools_for_child(self, role: str) -> list[Tool]:
        """Tools passed into AgentSession.create, excluding session-reserved tools.

        ``retrieve_archive`` is injected by ``create_session_tool_registry`` for
        each session, so passing the parent session's instance would violate the
        reserved-name guard.
        """

        return [tool for tool in self.tools_for_role(role) if tool.name != "retrieve_archive"]

    def _create_isolated_child_session(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        worktree: Worktree,
        session_id: str,
    ) -> AgentSession:
        """Create a child session whose permissions/tools are rooted at the worktree.

        The child gets its own ``PermissionManager`` (or a private clone of the
        parent's) whose ``project_root`` is the worktree path, so every path,
        shell, and git decision is evaluated against the isolated tree instead of
        the parent working directory.
        """

        permission_manager = self._child_permission_manager(worktree.path, mutation=True)
        # PROJECT sandbox keeps every file tool physically confined to the worktree
        # root even though the policy auto-allows in-tree writes.
        sandbox_access = SandboxAccess(mode=SandboxAccessMode.PROJECT)
        child = AgentSession.create(
            store=self.store,
            session_id=session_id,
            agents_md=self.agents_md,
            skill_catalog=self.skill_catalog,
            tools=self._worktree_child_tools(worktree.path, profile=profile, access=sandbox_access, for_registry=True),
            permission_manager=permission_manager,
            sandbox_access=sandbox_access,
        )
        # Background isolated coder has no interactive user, so per-write review
        # confirmations would deadlock the job.  The worktree diff is reviewed by the
        # parent instead, so disable the pausing prewrite-review path here.
        child.require_prewrite_review = False
        child.writer.append_session_metadata_updated(
            parent_session_id=request.parent_session_id,
            delegate_role=profile.role,
            delegate_task=request.task,
            worktree_path=str(worktree.path),
            worktree_branch=worktree.branch,
        )
        return child

    def _run_child_loop(self, child_session, prompt, tools, observer):
        """Run the child loop via the injected factory and harvest the runner.

        The child's ``cancellation_token`` is captured at call time so a parent
        turn cancelled mid-subagent aborts the child with ``AgentCancelledError``
        (P2-9). Generic child failures are swallowed into ``result=None`` so the
        delegate never breaks the parent loop; the caller builds the error result.
        """

        runner = self.child_runner_factory(
            session=child_session,
            tools=tools,
            observer=observer,
            cancellation_token=current_cancellation_token(),
        )
        try:
            result = asyncio.run(runner.run_user_turn(prompt))
            return result, runner
        except AgentCancelledError:
            raise
        except Exception:
            # Child session is deleted in the caller's `finally`, so the traceback
            # would otherwise be unrecoverable; keep the log signal while the
            # delegate still returns a generic failure result to the parent loop.
            logger.exception("subagent child loop failed; child session %s", child_session.session_id)
            return None, runner

    def _make_child_observer(self, progress_tracker: dict[str, Any] | None) -> TurnObserver:
        """返回子 agent 的 TurnObserver：后台委托写 Job.progress，前台写 progress_tracker。

        进度去向是每次 run 独立的决定，按线程局部 ``current_job_id()`` 分流：后台分支把
        进度写给对应 job 供 TUI 的 activity 面板读，前台分支写本次 run 创建的
        ``progress_tracker``（由 run() 决定并贯穿到本 run 结束）。tracker 按值闭包捕获，
        任何时刻清掉 ``self.foreground_progress`` 都不会让运行中的前台子 agent 崩溃。
        """

        def _report(state: dict[str, Any]) -> None:
            job_id = current_job_id()
            if job_id is not None:
                job = self.background_manager.get(job_id) if self.background_manager is not None else None
                if job is not None:
                    job.progress = state
                    return
            if progress_tracker is not None:
                progress_tracker.update(state)

        return TurnObserver(progress_callback=_report)

    def _attach_worktree_cleanup(self, manager: WorktreeManager, worktree: Worktree) -> None:
        """Attach worktree teardown to the current background job.

        A background coder runs inside an isolated worktree that is normally left
        for parent review. If a /recall abandons the job, the worktree would
        otherwise linger as an orphan; the attached cleanup removes it.
        """

        if self.background_manager is None:
            return
        job_id = current_job_id()
        if job_id is None:
            return
        job = self.background_manager.get(job_id)
        if job is not None:
            job.worktree_cleanup = lambda: manager.remove(worktree, force=True)

    def _child_permission_manager_for_inline(self) -> PermissionManager | None:
        """Clone the parent permission manager with a NETWORK_REQUEST grant added.

        Inline subagents (researcher, tester, reviewer) inherit the parent's policy
        and mode, but need web_search/fetch auto-allowed so they never pause for
        interactive confirmation.
        """
        if self.permission_manager is None:
            return None
        grants = PermissionGrantStore(grants=self.permission_manager.grants.list())
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
            policy=self.permission_manager.policy,
            grants=grants,
            mode=self.permission_manager.mode,
        )

    def _child_permission_manager(self, root, *, mutation: bool) -> PermissionManager:
        """Build an autonomous permission manager scoped to ``root``.

        The policy root is ``root`` (the worktree path for a coder, the project
        root otherwise), so every path/shell/git decision is evaluated against the
        isolated scope.  AGGRESSIVE mode auto-allows in-tree writes and a safe
        validation-command allow-list so a background subagent can make progress
        without an interactive user.  Anything still requiring confirmation
        (sensitive paths, dangerous shell, out-of-tree) is auto-DENIED rather than
        paused, so the subagent receives a clean denial and can try a safer
        approach instead of hanging.  Mutation-capable roles additionally get
        write/delete grants scoped to the isolated root.  A fresh grant store
        avoids inheriting parent-scoped grants.
        """

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

    def _background_child_permission_manager(self) -> PermissionManager | None:
        """Autonomous permissions for a background subagent with no worktree.

        Background researcher/reviewer/tester roles run inline (no worktree), but
        still have no interactive user, so they must not pause for confirmation.
        They have no write/delete tools, so no mutation grants are added; the
        network grant and AGGRESSIVE shell allow-list cover their approved actions.
        """

        if self.project_root is None:
            return self._child_permission_manager_for_inline()
        return self._child_permission_manager(self.project_root, mutation=False)

    def _worktree_child_tools(
        self,
        root,
        *,
        profile: SubagentProfile,
        access: SandboxAccess,
        for_registry: bool = False,
    ) -> list[Tool]:
        """Build fresh tools rooted at the worktree for the child's role.

        Tools are rebuilt (not reused from the parent) so their internal path
        sandboxes point at the worktree, not the parent cwd.  ``for_registry``
        excludes session-reserved tool names when passing into ``AgentSession``.
        """

        from lanscoder.tools.builtin import create_builtin_registry

        registry = create_builtin_registry(
            root,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=True,
            access=access,
        )
        allowed = profile.allowed_tool_names
        tools = [tool for tool in registry.tools() if tool.name in allowed and tool.name != "delegate"]
        if for_registry:
            tools = [tool for tool in tools if tool.name != "retrieve_archive"]
        return tools

    def _child_prompt(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        worktree: Worktree | None = None,
    ) -> str:
        hints = "\n".join(f"- {hint}" for hint in request.path_hints if str(hint).strip())
        summary = request.parent_summary.strip() if request.parent_summary else "(none provided)"
        if worktree is not None:
            root = str(worktree.path)
            isolation = (
                "You are running inside an ISOLATED git worktree. All edits stay on branch "
                f"{worktree.branch} and never touch the parent working tree. Implement the task, "
                "then summarize what you changed. Do not attempt to merge or push.\n"
            )
        else:
            root = str(self.project_root) if self.project_root is not None else "(current project root)"
            isolation = ""
        return (
            f"You are a LansCoder subagent with role: {profile.role}.\n"
            f"Role scope: {profile.description}\n"
            f"Project root: {root}\n"
            f"{isolation}"
            "Do not call delegate or spawn nested subagents.\n"
            "Return a compact final report with: summary, evidence, files changed, and risks.\n\n"
            f"Parent summary:\n{summary}\n\n"
            f"Path hints:\n{hints or '(none)'}\n\n"
            f"Task:\n{request.task}"
        )

    def _compose_isolated_summary(self, content: str, *, worktree: Worktree, diff: "WorktreeDiff") -> str:
        parts = [
            content,
            "",
            "--- isolated worktree ---",
            f"path: {worktree.path}",
            f"branch: {worktree.branch}",
            "diff:",
            diff.render(),
        ]
        return "\n".join(parts)
