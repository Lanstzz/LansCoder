"""子代理引擎:按角色创建子会话并执行子任务;支持前台/后台运行,以及 worktree 隔离执行。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
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
from lanscoder.observability.context import get_observation_id, get_trace_id
from lanscoder.observability.models import TraceScope
from lanscoder.observability.protocol import TraceRecorder
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import MainRequestOptions
from lanscoder.session.access import ChildSessionFactory, SessionAccessError, SessionAccessPolicy
from lanscoder.session.catalog import SessionCatalog
from lanscoder.storage.paths import project_id_for_path
from lanscoder.utils.cancellation import (
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


@dataclass(frozen=True, slots=True)
class _ChildTraceContext:
    """Parent identity captured at the delegate call boundary."""

    recorder: TraceRecorder | None
    parent_trace_id: str | None
    parent_observation_id: str | None


class SubagentEngine:
    """子代理执行引擎:管理子会话、角色工具集与前台进度,隔离执行时使用 worktree。"""

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
        trace_recorder: TraceRecorder | None = None,
        trace_id: str | None = None,
        trace_scope: TraceScope | None = None,
        allow_legacy_standalone_for_tests: bool = False,
    ) -> None:
        """注入子代理引擎依赖:存储、provider、角色工具集与子循环工厂。"""
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
        self.child_session_factory = self._build_child_session_factory()
        self.trace_recorder = trace_recorder
        self.trace_id = trace_id
        self.trace_scope = trace_scope
        self.allow_legacy_standalone_for_tests = allow_legacy_standalone_for_tests
        self.foreground_progress: dict[str, Any] | None = None

    def _build_child_session_factory(self) -> ChildSessionFactory | None:
        """Build the policy-backed child identity boundary when a project is known."""

        if self.project_root is None:
            return None
        return ChildSessionFactory(
            SessionAccessPolicy(
                self.project_root,
                journal=SessionCatalog(self.store.root),
            )
        )

    def profile(self, role: str) -> SubagentProfile | None:
        """按角色名返回子代理档案,未知角色返回 None。"""
        return SUBAGENT_PROFILES.get(str(role))

    def tools_for_role(self, role: str) -> list[Tool]:
        """返回某角色允许的工具集(剔除 delegate)。"""
        profile = self.profile(role)
        if profile is None:
            return []
        return [tool for tool in self.tools if tool.name in profile.allowed_tool_names and tool.name != "delegate"]

    def run(self, request: SubagentRequest) -> SubagentResult:
        """执行一次子代理请求:校验角色/后台限制,选择隔离或内联执行。"""
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
        """判断是否需要用 worktree 隔离执行。"""

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
        """在当前工作树内联执行子代理并保留完整 child journal。"""

        trace_context = self._child_trace_context(request)
        child_session = self.create_child_session(request, profile=profile, trace_context=trace_context)
        child_trace_id, child_scope = self._start_child_trace(
            child_session,
            request,
            trace_context=trace_context,
            worktree_metadata={},
        )
        child_trace_ended = False
        started = time.monotonic()
        try:
            prompt = self._child_prompt(request, profile=profile)
            result, runner, failure = self._run_child_loop(
                child_session,
                prompt,
                self.tools_for_role(request.role),
                self._make_child_observer(progress_tracker),
                trace_recorder=trace_context.recorder,
                trace_id=child_trace_id,
                trace_scope=child_scope,
            )
            usage = runner.usage_summary()
            response = result.response if result is not None else None
            if response is not None:
                if response.finish_reason == "interrupted":
                    self._end_child_trace(child_trace_id, status="cancelled", reason="interrupted")
                    child_trace_ended = True
                    raise AgentCancelledError()
                content = response.content.strip() or "Subagent finished without text output."
                self._end_child_trace(child_trace_id, status="completed", final_output=content)
                child_trace_ended = True
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
                self._end_child_trace(child_trace_id, status="failed", error="subagent paused for user input")
                child_trace_ended = True
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
            self._end_child_trace(child_trace_id, status="failed", error=failure or "child_loop_failed")
            child_trace_ended = True
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
        except AgentCancelledError:
            if not child_trace_ended:
                self._end_child_trace(child_trace_id, status="cancelled", reason="interrupted")
                child_trace_ended = True
            raise
        except Exception as exc:
            if not child_trace_ended:
                self._end_child_trace(child_trace_id, status="failed", error=exc)
                child_trace_ended = True
            raise
        finally:
            if not child_trace_ended and child_trace_id is not None:
                self._end_child_trace(child_trace_id, status="failed", error="child loop did not complete")

    def _run_isolated(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        progress_tracker: dict[str, Any] | None,
    ) -> SubagentResult:
        """在独立 worktree 中执行子代理,汇总变更差异并清理。"""

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

        trace_context = self._child_trace_context(request)
        child_trace_id: str | None = None
        child_trace_scope: TraceScope | None = None
        child_trace_ended = False
        try:
            child_session = self._create_isolated_child_session(
                request,
                profile=profile,
                worktree=worktree,
                session_id=session_id,
                trace_context=trace_context,
            )
            worktree_metadata = self._worktree_metadata(worktree)
            child_trace_id, child_trace_scope = self._start_child_trace(
                child_session,
                request,
                trace_context=trace_context,
                worktree_metadata=worktree_metadata,
            )
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
                    trace_recorder=trace_context.recorder,
                    trace_id=child_trace_id,
                    trace_scope=child_trace_scope,
                )
                usage = runner.usage_summary()
                diff = manager.diff(worktree)
                response = result.response if result is not None else None
                if response is not None:
                    if response.finish_reason == "interrupted":
                        self._end_child_trace(child_trace_id, status="cancelled", reason="interrupted")
                        child_trace_ended = True
                        raise AgentCancelledError()
                    content = response.content.strip() or "Subagent finished without text output."
                    summary = self._compose_isolated_summary(content, worktree=worktree, diff=diff)
                    self._end_child_trace(child_trace_id, status="completed", final_output=content)
                    child_trace_ended = True
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
                    self._end_child_trace(child_trace_id, status="failed", error="waiting_for_user_input")
                    child_trace_ended = True
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
                self._end_child_trace(child_trace_id, status="failed", error=failure or "child_loop_failed")
                child_trace_ended = True
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
            except AgentCancelledError:
                if not child_trace_ended:
                    self._end_child_trace(child_trace_id, status="cancelled", reason="interrupted")
                    child_trace_ended = True
                raise
            except Exception as exc:
                if not child_trace_ended:
                    self._end_child_trace(child_trace_id, status="failed", error=exc)
                    child_trace_ended = True
                raise
        except AgentCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - defensive: setup failures must not break parent loop
            if child_trace_id is not None and not child_trace_ended:
                self._end_child_trace(child_trace_id, status="failed", error=exc)
            return SubagentResult(
                ok=False,
                role=request.role,
                child_session_id=session_id,
                summary=f"隔离执行初始化失败：{exc}",
                error=str(exc),
                worktree_path=str(worktree.path),
                worktree_branch=worktree.branch,
            )
        finally:
            if child_trace_id is not None and not child_trace_ended:
                self._end_child_trace(child_trace_id, status="failed", error="isolated child did not complete")

    def create_child_session(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        trace_context: _ChildTraceContext | None = None,
        worktree_metadata: dict[str, Any] | None = None,
    ) -> AgentSession:
        """为子代理创建持久化 child session(按后台/前台选择权限配置)。"""
        session_id = new_session_id()
        trace_context = trace_context or self._child_trace_context(request)
        resolved_worktree_metadata = dict(worktree_metadata or {})
        session_metadata = self._resolve_child_session_metadata(
            request,
            profile=profile,
            trace_context=trace_context,
            worktree_metadata=resolved_worktree_metadata,
            session_id=session_id,
        )
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
            session_metadata=session_metadata,
        )
        return child

    def _resolve_child_session_metadata(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        trace_context: _ChildTraceContext,
        worktree_metadata: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        descriptor = self._create_child_descriptor(
            request,
            profile=profile,
            trace_context=trace_context,
            worktree_metadata=worktree_metadata,
            session_id=session_id,
        )
        if descriptor is not None:
            return dict(descriptor.metadata)

        if not self.allow_legacy_standalone_for_tests:
            raise SessionAccessError("subagent child session identity could not be authorized")

        return self._fallback_child_metadata(
            request,
            profile=profile,
            trace_context=trace_context,
            worktree_metadata=worktree_metadata,
        )

    def _create_child_descriptor(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        trace_context: _ChildTraceContext,
        worktree_metadata: dict[str, Any],
        session_id: str,
    ):
        factory = self.child_session_factory
        project_id = self._project_id(request.parent_session_id)
        parent_trace_id = trace_context.parent_trace_id
        if factory is None:
            if self.allow_legacy_standalone_for_tests:
                return None
            raise SessionAccessError("subagent child session requires a project root")
        if project_id is None:
            if self.allow_legacy_standalone_for_tests:
                return None
            raise SessionAccessError("subagent parent project_id is required")
        if parent_trace_id is None:
            if self.allow_legacy_standalone_for_tests:
                return None
            raise SessionAccessError("subagent parent_trace_id is required")
        try:
            return factory.create_child(
                parent_session_id=request.parent_session_id,
                parent_trace_id=parent_trace_id,
                project_id=project_id,
                worktree_metadata=worktree_metadata,
                delegate_role=profile.role,
                delegate_task=request.task,
                triggering_observation_id=trace_context.parent_observation_id,
                session_id=session_id,
            )
        except SessionAccessError:
            if self.allow_legacy_standalone_for_tests:
                return None
            raise

    def _fallback_child_metadata(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        trace_context: _ChildTraceContext,
        worktree_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "kind": "subagent",
            "parent_session_id": request.parent_session_id,
            "delegate_role": profile.role,
            "delegate_task": request.task,
            "project_id": self._project_id(request.parent_session_id),
            "worktree_metadata": dict(worktree_metadata),
        }
        if trace_context.parent_trace_id is not None:
            metadata["parent_trace_id"] = trace_context.parent_trace_id
        if trace_context.parent_observation_id is not None:
            metadata["parent_observation_id"] = trace_context.parent_observation_id
            metadata["triggering_observation_id"] = trace_context.parent_observation_id
        if worktree_metadata.get("path") is not None:
            metadata["worktree_path"] = str(worktree_metadata["path"])
        if worktree_metadata.get("branch") is not None:
            metadata["worktree_branch"] = str(worktree_metadata["branch"])
        return {key: value for key, value in metadata.items() if value is not None}

    def _project_id(self, parent_session_id: str) -> str | None:
        try:
            metadata = self.store.rebuild_session_view(parent_session_id).metadata
        except Exception:
            metadata = {}
        project_id = metadata.get("project_id")
        if project_id:
            return str(project_id)
        if self.project_root is not None:
            return project_id_for_path(self.project_root)
        return None

    def _child_trace_context(self, request: SubagentRequest) -> _ChildTraceContext:
        recorder = self.trace_recorder
        if recorder is None:
            writer = getattr(getattr(self.permission_coordinator, "session", None), "writer", None)
            recorder = getattr(writer, "trace_recorder", None)
        return _ChildTraceContext(
            recorder=recorder,
            parent_trace_id=request.parent_trace_id or get_trace_id() or self.trace_id,
            parent_observation_id=request.triggering_observation_id or get_observation_id(),
        )

    def _start_child_trace(
        self,
        child_session: AgentSession,
        request: SubagentRequest,
        *,
        trace_context: _ChildTraceContext,
        worktree_metadata: dict[str, Any],
    ) -> tuple[str | None, TraceScope | None]:
        recorder = trace_context.recorder
        branch = child_session.writer.branch_context
        if recorder is None or branch is None:
            return None, None
        scope = TraceScope(
            child_session.session_id,
            branch.branch_id,
            parent_trace_id=trace_context.parent_trace_id,
            parent_observation_id=trace_context.parent_observation_id,
        )
        data: dict[str, Any] = {
            "operation": "delegate",
            "delegate_role": str(request.role),
            "delegate_task": request.task,
            "parent_session_id": request.parent_session_id,
            "project_id": self._project_id(request.parent_session_id),
            "worktree_metadata": dict(worktree_metadata),
        }
        if trace_context.parent_observation_id is not None:
            data["parent_observation_id"] = trace_context.parent_observation_id
            data["triggering_observation_id"] = trace_context.parent_observation_id
        try:
            child_trace_id = recorder.start_trace(scope, data=data)
            if trace_context.parent_trace_id:
                link_data = {
                    "parent_session_id": request.parent_session_id,
                    "child_session_id": child_session.session_id,
                    "parent_observation_id": trace_context.parent_observation_id,
                    "triggering_observation_id": trace_context.parent_observation_id,
                }
                try:
                    recorder.link_trace(
                        trace_context.parent_trace_id,
                        child_trace_id,
                        relation="child",
                        data=link_data,
                        scope=scope,
                    )
                except TypeError:
                    recorder.link_trace(
                        trace_context.parent_trace_id,
                        child_trace_id,
                        relation="child",
                        data=link_data,
                    )
            child_session.set_trace_context(recorder, child_trace_id, scope)
            return child_trace_id, scope
        except Exception as exc:  # noqa: BLE001 - recorder failures are fail-open
            logger.debug("unable to start child trace for %s: %s", child_session.session_id, exc)
            return None, None

    def _end_child_trace(
        self,
        trace_id: str | None,
        *,
        status: str,
        final_output: Any = None,
        error: Any = None,
        reason: Any = None,
    ) -> None:
        recorder = self.trace_recorder
        if recorder is None:
            writer = getattr(getattr(self.permission_coordinator, "session", None), "writer", None)
            recorder = getattr(writer, "trace_recorder", None)
        if trace_id is None or recorder is None:
            return
        try:
            recorder.end_trace(
                trace_id,
                status=status,
                final_output=final_output,
                error=error,
                reason=reason,
            )
        except Exception:
            return

    def _supplied_tools_for_child(self, role: str) -> list[Tool]:
        """返回给子会话的工具集(剔除 retrieve_archive)。"""

        return [tool for tool in self.tools_for_role(role) if tool.name != "retrieve_archive"]

    def _create_isolated_child_session(
        self,
        request: SubagentRequest,
        *,
        profile: SubagentProfile,
        worktree: Worktree,
        session_id: str,
        trace_context: _ChildTraceContext | None = None,
    ) -> AgentSession:
        """为 worktree 隔离创建子会话,允许变更并关闭预写审查。"""

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
            session_metadata=self._resolve_child_session_metadata(
                request,
                profile=profile,
                trace_context=trace_context or self._child_trace_context(request),
                worktree_metadata=self._worktree_metadata(worktree),
                session_id=session_id,
            ),
        )
        child.require_prewrite_review = False
        return child

    @staticmethod
    def _worktree_metadata(worktree: Worktree) -> dict[str, Any]:
        return {
            "isolated": True,
            "path": str(worktree.path),
            "branch": worktree.branch,
        }

    def _run_child_loop(
        self,
        child_session,
        prompt,
        tools,
        observer,
        *,
        trace_recorder: TraceRecorder | None = None,
        trace_id: str | None = None,
        trace_scope: TraceScope | None = None,
    ):
        """在子会话上跑一次用户回合,返回 (结果, 运行器, 失败原因)。"""

        factory_kwargs = {
            "session": child_session,
            "tools": tools,
            "observer": observer,
            "cancellation_token": current_cancellation_token(),
            "trace_recorder": trace_recorder,
            "trace_id": trace_id,
            "trace_scope": trace_scope,
        }
        runner = self.child_runner_factory(**factory_kwargs)
        try:
            result = asyncio.run(runner.run_user_turn(prompt))
            return result, runner, None
        except AgentCancelledError:
            raise
        except Exception as exc:
            logger.exception("subagent child loop failed; child session %s", child_session.session_id)
            return None, runner, str(exc)

    def _make_child_observer(self, progress_tracker: dict[str, Any] | None) -> TurnObserver:
        """构造子代理观察者,把进度写入后台任务或前台跟踪器。"""

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
        """把 worktree 清理挂到当前后台任务上,任务结束时移除。"""

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
        """为 worktree 子会话构建按角色过滤的工具集。"""

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
        """构造子代理提示词(角色描述、隔离说明、父摘要与任务)。"""
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
        """把子代理结论与 worktree 差异拼接成最终摘要。"""
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
