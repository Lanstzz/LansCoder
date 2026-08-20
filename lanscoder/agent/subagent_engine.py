from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from lanscoder.agent.permission import PermissionCoordinator

logger = logging.getLogger(__name__)

DEFAULT_CHILD_LIMITS = AgentLoopLimits(max_tool_rounds=20, max_provider_calls=40, max_turn_seconds=600)


class SubagentEngine:

    def __init__(
        self,
        *,
        store: JsonlSessionStore,
        provider: ChatProvider,
        tools: list[Tool],
        project_root: str | Path | None = None,
        agents_md: str = "",
        skill_catalog: SkillCatalog | None = None,
        permission_coordinator: PermissionCoordinator,
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
        self.permission_coordinator = permission_coordinator
        self.request_options = request_options or MainRequestOptions()
        self.limits = limits or DEFAULT_CHILD_LIMITS
        self.background_manager = background_manager
        self.child_runner_factory = child_runner_factory
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

        child_session = self.create_child_session(request, profile=profile)
        started = time.monotonic()
        try:
            prompt = self._child_prompt(request, profile=profile)
            result, runner, failure = self._run_child_loop(
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
            if result is not None:
                return SubagentResult(
                    ok=False,
                    role=request.role,
                    child_session_id=child_session.session_id,
                    summary="subagent paused for user input",
                    error="subagent paused for user input",
                    total_tokens=usage["total_tokens"],
                    provider_calls=usage["provider_calls"],
                    elapsed_seconds=time.monotonic() - started,
                )
            summary = f"Subagent failed: {failure}" if failure else "subagent failed without a result"
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id=child_session.session_id,
                summary=summary,
                error=failure or "child_loop_failed",
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
                result, runner, failure = self._run_child_loop(
                    child_session,
                    prompt,
                    self._worktree_child_tools(
                        worktree.path,
                        profile=profile,
                        access=child_session.permission_coordinator.sandbox_access,
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
                if result is not None:
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
                        total_tokens=usage["total_tokens"],
                        provider_calls=usage["provider_calls"],
                        elapsed_seconds=time.monotonic() - started,
                    )
                summary = f"隔离 coder 执行失败：{failure}" if failure else "隔离 coder 执行失败：child loop 未产出结果。"
                return SubagentResult(
                    ok=False,
                    role=request.role,
                    child_session_id=session_id,
                    summary=summary,
                    error=failure or "child_loop_failed",
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
        if request.run_in_background:
            permission_manager = self.permission_coordinator.child_permission_manager(
                root=self.project_root,
                mutation=False,
                background=True,
            )
        else:
            permission_manager = self.permission_coordinator.child_permission_manager(
                root=None,
                mutation=False,
                background=False,
            )
        child = AgentSession.create(
            store=self.store,
            session_id=session_id,
            agents_md=self.agents_md,
            skill_catalog=self.skill_catalog,
            tools=self._supplied_tools_for_child(profile.role),
            permission_manager=permission_manager,
            sandbox_access=self.permission_coordinator.sandbox_access,
        )
        child.writer.append_session_metadata_updated(
            parent_session_id=request.parent_session_id,
            delegate_role=profile.role,
            delegate_task=request.task,
        )
        return child

    def _delete_child_session(self, session_id: str) -> None:
        try:
            self.store.delete_session(session_id)
        except Exception:  # noqa: BLE001 - cleanup must never break the parent loop
            pass

    def _supplied_tools_for_child(self, role: str) -> list[Tool]:

        return [tool for tool in self.tools_for_role(role) if tool.name != "retrieve_archive"]

    def _create_isolated_child_session(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        worktree: Worktree,
        session_id: str,
    ) -> AgentSession:

        permission_manager = self.permission_coordinator.child_permission_manager(
            root=worktree.path,
            mutation=True,
            background=False,
        )
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

        runner = self.child_runner_factory(
            session=child_session,
            tools=tools,
            observer=observer,
            cancellation_token=current_cancellation_token(),
        )
        try:
            result = asyncio.run(runner.run_user_turn(prompt))
            return result, runner, None
        except AgentCancelledError:
            raise
        except Exception as exc:
            logger.exception("subagent child loop failed; child session %s", child_session.session_id)
            return None, runner, str(exc)

    def _make_child_observer(self, progress_tracker: dict[str, Any] | None) -> TurnObserver:

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

        if self.background_manager is None:
            return
        job_id = current_job_id()
        if job_id is None:
            return
        job = self.background_manager.get(job_id)
        if job is not None:
            job.worktree_cleanup = lambda: manager.remove(worktree, force=True)

    def _worktree_child_tools(
        self,
        root,
        *,
        profile: SubagentProfile,
        access: SandboxAccess,
        for_registry: bool = False,
    ) -> list[Tool]:

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
