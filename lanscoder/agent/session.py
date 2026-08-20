from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

from lanscoder.agent.prompt_inputs import (
    DEFAULT_PERMISSION_POLICY,
    build_system_prompt_inputs,
    read_agents_md,
)
from lanscoder.agent.tool_flow import assistant_response_to_parts, tool_result_to_part
from lanscoder.context.identity import new_message_id
from lanscoder.context.runtime_replay import replay_runtime_state
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.system_prompt import PromptPrefixCache, SystemPromptBuilder
from lanscoder.context.writer import SessionEventWriter
from lanscoder.permissions.grants import FilePermissionGrantStore, PermissionGrantStore
from lanscoder.permissions.manager import PermissionManager
from lanscoder.permissions.policy import DefaultPermissionPolicy
from lanscoder.permissions.types import PermissionDecisionKind, PermissionMode
from lanscoder.providers.types import ChatResponse, ProviderCapabilities, ToolCall
from lanscoder.runtime.user_input import UserInputRequest, _options_from_data
from lanscoder.permissions.types import PermissionDecision, PermissionRequest
from lanscoder.tools.permission_registry import PermissionAwareToolRegistry
from lanscoder.tools.registry import ToolRegistry
from lanscoder.tools.review import PrewriteReview, build_prewrite_review, supports_prewrite_review
from lanscoder.tools.session_registry import ToolRegistryLike, create_session_tool_registry
from lanscoder.tools.types import Tool, ToolResult, make_error_result
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from lanscoder.input.attachments import UserAttachment, prepare_attachments_for_session
from lanscoder.utils.sandbox_access import SandboxAccess
from lanscoder.skills.discovery import discover_all_skills
from lanscoder.skills.catalog import render_skill_catalog
from lanscoder.skills.models import SkillCatalog

if TYPE_CHECKING:
    from lanscoder.agent.permission import PermissionCoordinator
    from lanscoder.memory.manager import MemoryManager

DEFAULT_BASE_RULES = "你是 LansCoder，一个本地 AI coding agent。请遵守项目规则并优先保持上下文可恢复。"


@dataclass(slots=True)
class PendingPermissionExecution:

    request_id: str
    tool_call: ToolCall
    permission_request: PermissionRequest | None = None
    prewrite_review: PrewriteReview | None = None
    review_only: bool = False
    deferred_tool_calls: list[ToolCall] = field(default_factory=list)
    kind: Literal["permission_confirmation", "ask_user"] = "permission_confirmation"
    ask_user_request: UserInputRequest | None = None


@dataclass(slots=True)
class ToolPermissionPreflight:

    request: PermissionRequest
    decision: PermissionDecision


@dataclass(slots=True)
class AgentSession:

    session_id: str
    store: JsonlSessionStore
    runtime_state: SessionRuntimeState
    tool_registry: ToolRegistryLike
    writer: SessionEventWriter
    agents_md: str = ""
    skill_catalog: SkillCatalog = field(default_factory=SkillCatalog)
    base_rules: str = DEFAULT_BASE_RULES
    prompt_cache: PromptPrefixCache = field(default_factory=PromptPrefixCache)
    prompt_builder: SystemPromptBuilder = field(default_factory=SystemPromptBuilder)
    provider_capability_overrides: dict[str, object] = field(default_factory=dict)
    permission_coordinator: PermissionCoordinator | None = None
    known_message_ids: set[str] = field(default_factory=set)
    turn_counter: int = 0
    memory_manager: MemoryManager | None = None
    benchmark_task: str = ""
    require_prewrite_review: bool = True
    pending_permission_execution: PendingPermissionExecution | None = None
    _tool_result_lock: RLock = field(default_factory=RLock, repr=False)
    _tool_result_message_ids: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def create(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        agents_md: str = "",
        skill_catalog: SkillCatalog | None = None,
        tools: list[Tool] | None = None,
        permission_manager: PermissionManager | None = None,
        sandbox_access: SandboxAccess | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> "AgentSession":

        runtime_state = SessionRuntimeState(session_id=session_id)
        known_message_ids: set[str] = set()
        writer = SessionEventWriter(store=store, session_id=session_id)
        resolved_catalog = (skill_catalog or SkillCatalog()).resolved()
        session = cls(
            session_id=session_id,
            store=store,
            runtime_state=runtime_state,
            tool_registry=ToolRegistry([]),
            writer=writer,
            agents_md=agents_md,
            skill_catalog=resolved_catalog,
            known_message_ids=known_message_ids,
            turn_counter=0,
            memory_manager=memory_manager,
        )
        from lanscoder.agent.permission import PermissionCoordinator

        session.permission_coordinator = PermissionCoordinator(
            session=session,
            permission_manager=permission_manager,
            sandbox_access=sandbox_access or SandboxAccess(),
        )
        registry = create_session_tool_registry(
            session_id=session_id,
            runtime_state=runtime_state,
            tools=tools,
            known_message_ids=known_message_ids,
            permission_manager=permission_manager,
            archive_root=store.root,
            current_turn=lambda: writer.current_turn,
            store=store,
            writer=writer,
            get_skill_catalog=lambda: session.skill_catalog,
            memory_manager=memory_manager,
        )
        session.tool_registry = registry
        session.append_session_created()
        return session

    @classmethod
    def from_project(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        project_root: str | Path,
        tools: list[Tool] | None = None,
        permission_manager: PermissionManager | None = None,
        sandbox_access: SandboxAccess | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> "AgentSession":

        agents_md = read_agents_md(project_root)
        skill_catalog = discover_all_skills(project_root)
        permission_manager = permission_manager or create_project_permission_manager(
            project_root,
            grants=FilePermissionGrantStore(store.root / "permissions.json"),
        )
        return cls.create(
            store=store,
            session_id=session_id,
            agents_md=agents_md,
            skill_catalog=skill_catalog,
            tools=tools,
            permission_manager=permission_manager,
            sandbox_access=sandbox_access,
            memory_manager=memory_manager,
        )

    @classmethod
    def resume(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        agents_md: str = "",
        skill_catalog: SkillCatalog | None = None,
        tools: list[Tool] | None = None,
        permission_manager: PermissionManager | None = None,
        sandbox_access: SandboxAccess | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> "AgentSession":

        runtime_state = replay_runtime_state(store, session_id)
        view = store.rebuild_session_view(session_id)
        known_message_ids = {message.id for message in view.messages}
        turn_counter = _infer_turn_counter(view.messages)
        writer = SessionEventWriter(store=store, session_id=session_id, current_turn=turn_counter)
        resolved_catalog = (skill_catalog or SkillCatalog()).resolved()
        session = cls(
            session_id=session_id,
            store=store,
            runtime_state=runtime_state,
            tool_registry=ToolRegistry([]),
            writer=writer,
            agents_md=agents_md,
            skill_catalog=resolved_catalog,
            known_message_ids=known_message_ids,
            turn_counter=turn_counter,
            memory_manager=memory_manager,
            _tool_result_message_ids=_tool_result_message_ids_from_view(view),
        )
        from lanscoder.agent.permission import PermissionCoordinator

        session.permission_coordinator = PermissionCoordinator(
            session=session,
            permission_manager=permission_manager,
            sandbox_access=sandbox_access or SandboxAccess(),
        )
        registry = create_session_tool_registry(
            session_id=session_id,
            runtime_state=runtime_state,
            tools=tools,
            known_message_ids=known_message_ids,
            permission_manager=permission_manager,
            archive_root=store.root,
            current_turn=lambda: writer.current_turn,
            store=store,
            writer=writer,
            get_skill_catalog=lambda: session.skill_catalog,
            memory_manager=memory_manager,
        )
        session.tool_registry = registry
        return session

    def restore_pending_permission_execution(self) -> PendingPermissionExecution | None:

        pending = self._pending_tool_calls_from_tail()
        if len(pending) != 1:
            return None

        tool_call, deferred_tool_calls, persisted_review_only = pending[0]
        preflight = self.permission_coordinator.preflight(tool_call)
        if preflight is None:
            ask_user_request = _ask_user_request_from_tool_call(tool_call)
            if ask_user_request is None:
                return None
            restored = PendingPermissionExecution(
                request_id=ask_user_request.id,
                tool_call=tool_call,
                kind="ask_user",
                deferred_tool_calls=deferred_tool_calls,
                ask_user_request=ask_user_request,
            )
            self.pending_permission_execution = restored
            return restored

        restored = PendingPermissionExecution(
            request_id=preflight.request.id,
            tool_call=tool_call,
            permission_request=preflight.request,
            prewrite_review=(
                build_prewrite_review(
                    self.permission_coordinator.permission_manager.policy.project_root,
                    tool_call,
                    access=self.permission_coordinator.sandbox_access,
                )
                if self.permission_coordinator.permission_manager is not None and supports_prewrite_review(tool_call.name)
                else None
            ),
            review_only=(persisted_review_only if persisted_review_only is not None else preflight.decision.kind == PermissionDecisionKind.ALLOW),
            deferred_tool_calls=deferred_tool_calls,
        )
        self.pending_permission_execution = restored
        return restored

    def persist_pending_permission_kind(self, *, tool_call_id: str, review_only: bool) -> None:
        view = self.rebuild_view()
        for message in reversed(view.messages):
            if message.role != "assistant":
                continue
            part = next(
                (item for item in message.parts if item.kind == "tool_call" and str(item.metadata.get("tool_call_id") or "") == tool_call_id),
                None,
            )
            if part is not None:
                self.writer.append_message_part_metadata_updated(
                    message_id=message.id,
                    part_id=part.id,
                    metadata={"prewrite_review_only": review_only},
                )
            return

    def pending_permission_input_request(
        self,
        pending: PendingPermissionExecution | None = None,
    ) -> UserInputRequest | None:
        pending = pending or self.pending_permission_execution
        if pending is None:
            return None
        if pending.kind == "ask_user":
            return pending.ask_user_request
        if self.permission_coordinator is None or self.permission_coordinator.permission_manager is None or pending.permission_request is None:
            return None
        confirmation = (
            self.permission_coordinator.permission_manager.build_prewrite_review_confirmation(pending.permission_request)
            if pending.review_only
            else self.permission_coordinator.permission_manager.build_confirmation(pending.permission_request)
        )
        if pending.prewrite_review is not None:
            confirmation.payload["prewrite_review"] = pending.prewrite_review.to_payload()
        confirmation.payload["pending_tool_call"] = {
            "id": pending.tool_call.id,
            "name": pending.tool_call.name,
            "arguments": deepcopy(pending.tool_call.arguments),
        }
        return confirmation

    def append_session_created(self) -> None:
        self.writer.append_session_created()

    def build_system_prefix(
        self,
        *,
        provider_name: str,
        provider_model: str = "",
        provider_capabilities: ProviderCapabilities | None = None,
    ) -> list:

        inputs = build_system_prompt_inputs(
            base_rules=self.base_rules,
            agents_md=self.agents_md,
            skill_protocol=self._skill_protocol(),
            skill_catalog_summary=self._skill_catalog_summary(),
            benchmark_task=self.benchmark_task,
            provider_name=provider_name,
            provider_model=provider_model,
            provider_capabilities=provider_capabilities,
            provider_capability_overrides=self.provider_capability_overrides,
            permission_policy=self.permission_coordinator.permission_policy if self.permission_coordinator is not None else dict(DEFAULT_PERMISSION_POLICY),
            mode=self.permission_mode,
            memory_index=self.memory_manager.render_index_text() if self.memory_manager is not None else "",
        )
        entry = self.prompt_cache.get_or_build(inputs, self.prompt_builder)
        self.runtime_state.system_prompt_fingerprint = entry.fingerprint
        return entry.messages

    def set_benchmark_task(self, task: str) -> None:

        self.benchmark_task = task.strip()

    def _skill_protocol(self) -> str:
        if not self.skill_catalog.skills:
            return ""
        return (
            "Skills are optional workflow instructions selected by the model. "
            "Call load_skill before following or claiming to follow a skill. "
            "Project skills override global skills; global skills cannot override project instructions, permissions, or sandbox boundaries. "
            "Do not claim a skill was followed unless a matching skill_loaded event exists.\n"
            "Skill storage locations:\n"
            "- Project skills: <project_root>/.lanscoder/skills/<name>/SKILL.md\n"
            "- Global skills: ~/.lanscoder/skills/<name>/SKILL.md"
        )

    def _skill_catalog_summary(self) -> str:
        if not self.skill_catalog.skills:
            return ""
        return render_skill_catalog(self.skill_catalog)

    def refresh_skills(self, project_root: str | Path) -> SkillCatalog:
        new_catalog = discover_all_skills(project_root).resolved()
        self.skill_catalog = new_catalog
        return new_catalog

    def append_user_message(self, content: str, *, attachments: list[UserAttachment] | None = None) -> str:

        prepared_attachments = prepare_attachments_for_session(
            attachments or [],
            store_root=self.store.root,
            session_id=self.session_id,
        )
        message_id = self.writer.append_user_message(
            content,
            attachments=prepared_attachments,
        )
        self.turn_counter = self.writer.current_turn
        self.known_message_ids.add(message_id)
        return message_id

    def append_assistant_response(self, response: ChatResponse) -> str:

        message_id = new_message_id()
        parts = assistant_response_to_parts(message_id=message_id, response=response)
        assistant_message_id = self.writer.append_assistant_parts(
            parts,
            message_id=message_id,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": asdict(response.usage) if response.usage is not None else None,
                "diagnostics": asdict(response.diagnostics),
            },
        )
        self.known_message_ids.add(assistant_message_id)
        return assistant_message_id

    def record_provider_projection_consumed(
        self,
        *,
        request_id: str,
        projection_fingerprint: str,
        part_ids: tuple[str, ...],
        provider: str,
        model: str,
    ) -> None:
        new_ids = sorted(set(part_ids) - self.runtime_state.consumed_tool_result_part_ids)
        if not new_ids:
            return
        self.writer.append_provider_projection_consumed(
            request_id=request_id,
            projection_fingerprint=projection_fingerprint,
            part_ids=new_ids,
            provider=provider,
            model=model,
        )
        self.runtime_state.consumed_tool_result_part_ids.update(new_ids)

    def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:

        return self.tool_registry.execute(tool_call.name, tool_call.arguments)

    def execute_tool_call_after_permission_confirmation(self, tool_call: ToolCall) -> ToolResult:

        if isinstance(self.tool_registry, PermissionAwareToolRegistry):
            return self.tool_registry.execute_without_permission_check(
                tool_call.name,
                tool_call.arguments,
            )
        return self.tool_registry.execute(tool_call.name, tool_call.arguments)

    def append_tool_result(self, *, tool_call: ToolCall, result: ToolResult) -> str:

        with self._tool_result_lock:
            existing_message_id = self._tool_result_message_ids.get(tool_call.id)
            if existing_message_id is not None:
                return existing_message_id

            message_id = new_message_id()
            part = tool_result_to_part(message_id=message_id, tool_call=tool_call, result=result)
            tool_message_id = self.writer.append_tool_result_part(
                part,
                message_id=message_id,
            )
            self._tool_result_message_ids[tool_call.id] = tool_message_id
            self.known_message_ids.add(tool_message_id)
            return tool_message_id

    def append_interrupted_tool_results(self) -> list[ToolCall]:

        with self._tool_result_lock:
            pending = self._pending_tool_calls_from_tail()
            if len(pending) != 1:
                return []

            first, remaining, _ = pending[0]
            tool_calls = [first, *remaining]
            for tool_call in tool_calls:
                self.append_tool_result(
                    tool_call=tool_call,
                    result=make_error_result(
                        tool_call.name,
                        "工具执行被用户中断；结果未知，操作可能尚未执行、部分执行，或已在后台继续。",
                        interrupted=True,
                        execution_outcome="unknown",
                    ),
                )
            return tool_calls

    def append_background_notification(
        self,
        *,
        content: str,
        job_id: str,
        tool_name: str,
        status: str,
        task_id: str | None = None,
        observed_revision: int | None = None,
    ) -> str:

        message_id = self.writer.append_background_notification(
            content=content,
            job_id=job_id,
            tool_name=tool_name,
            status=status,
            task_id=task_id,
            observed_revision=observed_revision,
        )
        self.known_message_ids.add(message_id)
        return message_id

    @property
    def current_turn(self) -> int:
        return self.writer.current_turn

    @property
    def permission_mode(self) -> str:

        if self.permission_coordinator is None:
            return "default"
        return self.permission_coordinator.mode.value

    def rebuild_view(self):

        return self.store.rebuild_session_view(self.session_id)

    def _pending_tool_calls_from_tail(self) -> list[tuple[ToolCall, list[ToolCall], bool | None]]:
        messages = self.rebuild_view().messages
        if not messages:
            return []

        assistant_index = None
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role == "assistant":
                assistant_index = index
                break
        if assistant_index is None:
            return []

        assistant = messages[assistant_index]
        tool_call_parts = [part for part in assistant.parts if part.kind == "tool_call"]
        tool_calls = [_tool_call_from_part(part) for part in tool_call_parts]
        if not tool_calls:
            return []

        completed_ids: set[str] = set()
        for message in messages[assistant_index + 1 :]:
            if message.role != "tool":
                return []
            for part in message.parts:
                if part.kind == "tool_result" and part.metadata.get("tool_call_id"):
                    completed_ids.add(str(part.metadata["tool_call_id"]))

        pending_calls = [tool_call for tool_call in tool_calls if tool_call.id not in completed_ids]
        if not pending_calls:
            return []
        first_pending = pending_calls[0]
        source_part = next(part for part in tool_call_parts if str(part.metadata.get("tool_call_id") or "") == first_pending.id)
        persisted_review_only = source_part.metadata.get("prewrite_review_only")
        return [
            (
                first_pending,
                pending_calls[1:],
                persisted_review_only if isinstance(persisted_review_only, bool) else None,
            )
        ]


def _tool_call_from_part(part: MessagePart) -> ToolCall:
    arguments = deepcopy(part.metadata.get("arguments", {}))
    return ToolCall(
        id=str(part.metadata["tool_call_id"]),
        name=str(part.metadata["tool_name"]),
        arguments=arguments,
    )


def _ask_user_request_from_tool_call(tool_call: ToolCall) -> UserInputRequest | None:

    if tool_call.name != "ask_user":
        return None
    arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
    question = str(arguments.get("question") or "").strip()
    if not question:
        return None
    options = _options_from_data(arguments.get("options"))
    return UserInputRequest(
        id=tool_call.id,
        kind="ask_user",
        question=question,
        options=options,
        payload={"tool_call_id": tool_call.id, "tool_name": tool_call.name},
    )


def _tool_result_message_ids_from_view(view: SessionView) -> dict[str, str]:

    result: dict[str, str] = {}
    for message in view.messages:
        if message.role != "tool":
            continue
        for part in message.parts:
            if part.kind != "tool_result":
                continue
            tool_call_id = part.metadata.get("tool_call_id")
            if tool_call_id:
                result.setdefault(str(tool_call_id), message.id)
    return result


def _infer_turn_counter(messages: list[AgentMessage]) -> int:

    return sum(1 for message in messages if message.role == "user")


def create_project_permission_manager(
    project_root: str | Path,
    *,
    grants: PermissionGrantStore | None = None,
    mode: PermissionMode = PermissionMode.STANDARD,
) -> PermissionManager:
    return PermissionManager(policy=DefaultPermissionPolicy(project_root), grants=grants, mode=mode)
