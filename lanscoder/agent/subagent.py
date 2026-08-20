"""Subagent runner for the Phase 2 delegate tool.

The first implementation keeps the boundary deliberately small:

- child sessions are fresh and metadata-tagged;
- tool access is profile restricted;
- children do not receive the delegate tool, preventing recursion;
- foreground execution returns only a compact summary to the parent;
- background execution is handled by the Phase 1 generic async runtime.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from lanscoder.agent.background import BackgroundJobManager, current_job_id
from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.agent.session import AgentSession
from lanscoder.agent.worktree import Worktree, WorktreeDiff, WorktreeError, WorktreeManager
from lanscoder.context.identity import new_session_id
from lanscoder.context.store import JsonlSessionStore
from lanscoder.permissions.grants import PermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import PermissionAction, PermissionGrant, PermissionMode, PermissionScopeType
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import MainRequestOptions
from lanscoder.skills.models import SkillCatalog
from lanscoder.subagent.types import SUBAGENT_PROFILES, SubagentProfile, SubagentRequest, SubagentResult
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


class SubagentRunner:
    """Create and run isolated child AgentLoop sessions for delegate."""

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

        if self._needs_worktree(request, profile=profile):
            return self._run_isolated(request, profile=profile)
        return self._run_inline(request, profile=profile)

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

    def _run_inline(self, request: SubagentRequest, *, profile: SubagentProfile) -> SubagentResult:
        """Original Phase 2 behaviour: run the child against the parent-rooted tools."""

        child_session = self.create_child_session(request, profile=profile)
        try:
            prompt = self._child_prompt(request, profile=profile)
            from lanscoder.agent.loop import AgentLoop

            loop = AgentLoop(
                session=child_session,
                provider=self.provider,
                tools=self.tools_for_role(request.role),
                limits=self.limits,
                request_options=self.request_options,
                background_manager=None,
                enable_delegate_tool=False,
                progress_callback=self._make_progress_callback(),
            )
            try:
                result = asyncio.run(loop.run_user_turn(prompt))
                response = result.response
                if response is None:
                    raise RuntimeError("subagent paused for user input")
            except Exception as exc:  # noqa: BLE001 - delegate must return a tool result, not break parent loop
                return SubagentResult(
                    ok=False,
                    role=request.role,
                    child_session_id=child_session.session_id,
                    summary=f"Subagent failed: {exc}",
                    error=str(exc),
                )
            content = response.content.strip() or "Subagent finished without text output."
            return SubagentResult(
                ok=True,
                role=request.role,
                child_session_id=child_session.session_id,
                summary=content,
            )
        finally:
            self._delete_child_session(child_session.session_id)

    def _run_isolated(self, request: SubagentRequest, *, profile: SubagentProfile) -> SubagentResult:
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
                from lanscoder.agent.loop import AgentLoop

                loop = AgentLoop(
                    session=child_session,
                    provider=self.provider,
                    tools=self._worktree_child_tools(worktree.path, profile=profile, access=child_session.sandbox_access),
                    limits=self.limits,
                    request_options=self.request_options,
                    background_manager=None,
                    enable_delegate_tool=False,
                    progress_callback=self._make_progress_callback(),
                )
                try:
                    result = asyncio.run(loop.run_user_turn(prompt))
                    response = result.response
                    if response is None:
                        diff = manager.diff(worktree)
                        return SubagentResult(
                            ok=False,
                            role=request.role,
                            child_session_id=session_id,
                            summary="隔离 coder 等待用户输入，无法在后台继续。",
                            error="waiting_for_user_input",
                            files_changed=diff.files_changed,
                            worktree_path=str(worktree.path),
                            worktree_branch=worktree.branch,
                            diff_summary=diff.render(),
                        )
                except Exception as exc:  # noqa: BLE001 - never break the parent loop
                    diff = manager.diff(worktree)
                    return SubagentResult(
                        ok=False,
                        role=request.role,
                        child_session_id=session_id,
                        summary=f"隔离 coder 执行失败：{exc}",
                        error=str(exc),
                        files_changed=diff.files_changed,
                        worktree_path=str(worktree.path),
                        worktree_branch=worktree.branch,
                        diff_summary=diff.render(),
                    )
                diff = manager.diff(worktree)
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
                )
            finally:
                self._delete_child_session(session_id)
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

    def _make_progress_callback(self):
        """Return a progress callback that updates the current background job.

        Uses the thread-local ``current_job_id()`` to discover which background
        job this subagent belongs to, then writes progress data into
        ``BackgroundJob.progress`` for the TUI to read.
        """
        if self.background_manager is None:
            return None
        job_id = current_job_id()
        if job_id is None:
            return None

        def _report(state: dict[str, Any]) -> None:
            job = self.background_manager.get(job_id)
            if job is not None:
                job.progress = state

        return _report

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
