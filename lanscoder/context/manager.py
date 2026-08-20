"""上下文窗口管理:触发时压缩上下文,先后走规则式压缩与 LLM 摘要,失败时回退到更强压缩或硬截断。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Protocol

from lanscoder.context.checkpoint import Checkpoint
from lanscoder.context.compaction import CompactionEvent, CompactionPipeline, CompactionRequest, CompactionResult
from lanscoder.context.context_builder import InvalidCheckpointBoundaryError
from lanscoder.context.fallback import CompactFallbackPolicy, FallbackStep
from lanscoder.context.identity import session_view_fingerprint
from lanscoder.context.llm_compact import (
    LlmCompactCandidate,
    LlmCompactEvent,
    LlmCompactRequest,
    source_fingerprint_for_view,
)
from lanscoder.context.models import AgentMessage, SessionView
from lanscoder.context.provider_summarizer import select_compaction_boundary
from lanscoder.context.runtime_state import SessionRuntimeState, auto_compact_circuit_is_open
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.token_budget import ContextBudget
from lanscoder.context.tool_sequence import InvalidToolCallSequenceError
from lanscoder.context.triggers import ContextCompactionConfig, evaluate_context_triggers
from lanscoder.context.writer import SessionEventWriter


class ContextWindowTrigger(StrEnum):
    AUTO = "auto"
    PROMPT_TOO_LONG = "prompt_too_long"
    MANUAL = "manual"


class ContextCompactMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


ManagerStatus = Literal["success", "skipped", "failed"]


class ProgrammaticCompactor(Protocol):
    """规则式压缩器接口。"""

    def compact(self, request: CompactionRequest): ...


class L3Compactor(Protocol):
    """LLM 摘要压缩器接口:生成候选检查点并提交。"""

    def generate_candidate(self, request: LlmCompactRequest) -> LlmCompactCandidate: ...

    def commit_candidate(
        self,
        candidate: LlmCompactCandidate,
        *,
        runtime_state: SessionRuntimeState,
    ) -> Checkpoint: ...


@dataclass(slots=True)
class ContextCompactRequest:
    """一次压缩请求:视图、运行时状态、预算与触发原因。"""

    view: SessionView
    runtime_state: SessionRuntimeState
    budget: ContextBudget
    estimate_budget: Callable[[SessionView], ContextBudget]
    trigger: ContextWindowTrigger | str = ContextWindowTrigger.AUTO
    mode: ContextCompactMode | str = ContextCompactMode.AUTO
    current_turn: int = 0
    target_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextCompactResult:
    """压缩结果:状态、原因、前后 token 数与压缩事件/回退记录。"""

    status: ManagerStatus
    reason: str
    view: SessionView
    before_tokens: int
    after_tokens: int
    programmatic_event: CompactionEvent | None = None
    l3_event: LlmCompactEvent | None = None
    fallback_steps: list[dict[str, object]] | None = None
    final_failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _CandidateOutcome:
    """L3 候选的产出:候选、事件与压缩后视图及 token 数。"""

    candidate: LlmCompactCandidate
    event: LlmCompactEvent
    view: SessionView
    input_tokens: int


@dataclass(frozen=True, slots=True)
class _HardTruncateOutcome:
    """硬截断的结果状态。"""

    status: Literal["success", "over_budget", "nothing_to_drop"]
    result: ContextCompactResult | None = None


@dataclass(slots=True)
class ContextWindowManager:
    """上下文窗口管理器:判断是否压缩,并编排 programmatic/L3/回退/硬截断等压缩路径。"""

    store: JsonlSessionStore
    pipeline: ProgrammaticCompactor | None = None
    l3_service: L3Compactor | None = None
    config: ContextCompactionConfig | None = None
    fallback_policy: CompactFallbackPolicy = CompactFallbackPolicy()

    def __post_init__(self) -> None:
        """缺省时补全压缩配置与规则式压缩管线。"""
        if self.config is None:
            self.config = ContextCompactionConfig()
        if self.pipeline is None:
            self.pipeline = CompactionPipeline(
                root=self.store.root,
                large_tool_result_tokens=self.config.large_tool_result_tokens,
                cold_preview_chars=self.config.cold_preview_chars,
            )

    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult:
        """入口:评估触发条件,按需压缩(programmatic → L3 → 回退)并返回结果。"""
        trigger = ContextWindowTrigger(request.trigger)
        mode = ContextCompactMode(request.mode)
        before_tokens = request.budget.input_tokens
        input_fingerprint = session_view_fingerprint(request.view)
        auto_failure_count_before = request.runtime_state.auto_compact_failure_count

        decision = evaluate_context_triggers(
            request.view,
            self.config,
            input_tokens=before_tokens,
            high_watermark=request.budget.high_watermark,
            low_watermark=request.budget.low_watermark,
        )
        if trigger == ContextWindowTrigger.AUTO and not decision.should_compact:
            return self._unchanged("skipped", "under_threshold", request, before_tokens)
        if trigger == ContextWindowTrigger.AUTO and mode == ContextCompactMode.AUTO and auto_compact_circuit_is_open(request.runtime_state):
            return self._unchanged("skipped", "circuit_open", request, before_tokens)
        if trigger == ContextWindowTrigger.AUTO and request.runtime_state.last_no_effect_compaction_fingerprint == input_fingerprint:
            return self._unchanged("skipped", "skipped_no_effect", request, before_tokens)

        target_tokens = _target_tokens(request, trigger)
        if request.budget.fixed_tokens >= request.budget.low_watermark:
            return ContextCompactResult(
                status="failed",
                reason="fixed_context_over_budget",
                view=request.view,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                final_failure_reason="fixed_context_over_budget",
            )

        programmatic = self.pipeline.compact(
            CompactionRequest(
                view=request.view,
                target_tokens=target_tokens,
                current_turn=request.current_turn,
                estimate_tokens=lambda candidate: request.estimate_budget(candidate).input_tokens,
                consumed_tool_result_part_ids=frozenset(request.runtime_state.consumed_tool_result_part_ids),
                l2_result_target_tokens=self.config.l2_result_target_tokens,
                force_route_current_text=_force_route_current_text_for_trigger(trigger),
            )
        )
        after_tokens = request.estimate_budget(programmatic.view).input_tokens

        if trigger == ContextWindowTrigger.AUTO and programmatic.event.noop and after_tokens < target_tokens:
            request.runtime_state.last_no_effect_compaction_fingerprint = input_fingerprint
            SessionEventWriter(store=self.store, session_id=request.view.session_id).append_compaction_skipped(
                trigger=trigger.value,
                input_fingerprint=input_fingerprint,
                reason="skipped_no_effect",
            )
            return ContextCompactResult(
                status="skipped",
                reason="skipped_no_effect",
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                programmatic_event=programmatic.event,
            )

        self._record_programmatic_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=programmatic.event,
        )
        if after_tokens < target_tokens and trigger != ContextWindowTrigger.PROMPT_TOO_LONG:
            self._record_auto_success_if_needed(request=request, mode=mode)
            return ContextCompactResult(
                status="success",
                reason=_result_reason(trigger=trigger, auto_reason=decision.reason),
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                programmatic_event=programmatic.event,
            )

        if self.l3_service is None:
            return self._final_l3_failure(
                request=request,
                trigger=trigger,
                mode=mode,
                target_tokens=target_tokens,
                programmatic=programmatic,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                before_failure_count=auto_failure_count_before,
                event=LlmCompactEvent(
                    status="failed",
                    source_fingerprint=programmatic.event.input_fingerprint,
                    failure_reason="l3_service_missing",
                ),
                reason="l3_service_missing",
            )

        outcome = self._generate_validate_commit(
            request=request,
            l3_request=LlmCompactRequest(
                view=programmatic.view,
                runtime_state=request.runtime_state,
                consumed_tool_result_part_ids=frozenset(request.runtime_state.consumed_tool_result_part_ids),
                mode=mode.value,
                current_turn=request.current_turn,
                recent_turn_window=self.config.recent_turn_window,
            ),
            target_tokens=target_tokens,
        )
        if outcome.event.status != "success":
            return self._run_fallback(
                request=request,
                trigger=trigger,
                mode=mode,
                target_tokens=target_tokens,
                programmatic=programmatic,
                outcome=outcome,
                before_failure_count=auto_failure_count_before,
            )

        self._record_l3_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=outcome.event,
        )
        self._record_auto_success_if_needed(request=request, mode=mode)
        return ContextCompactResult(
            status="success",
            reason=_result_reason(trigger=trigger, auto_reason=decision.reason),
            view=outcome.view,
            before_tokens=before_tokens,
            after_tokens=outcome.input_tokens,
            programmatic_event=programmatic.event,
            l3_event=outcome.event,
        )

    def _generate_validate_commit(
        self,
        *,
        request: ContextCompactRequest,
        l3_request: LlmCompactRequest,
        target_tokens: int,
    ) -> _CandidateOutcome:
        """生成 L3 摘要候选,校验预算与工具序列后提交,返回产出。"""
        candidate = self.l3_service.generate_candidate(l3_request)
        event = candidate.event
        if event.status != "success" or candidate.checkpoint is None:
            reason = event.failure_reason or event.status
            if reason == "unconsumed_boundary" and request.budget.input_tokens > request.budget.input_capacity:
                reason = "unconsumed_result_over_budget"
            failed = replace(event, status="failed", failure_reason=reason, final_failure_reason=reason)
            return _CandidateOutcome(
                candidate=candidate,
                event=failed,
                view=l3_request.view,
                input_tokens=request.estimate_budget(l3_request.view).input_tokens,
            )

        candidate_view = _view_with_checkpoint(l3_request.view, candidate.checkpoint)
        try:
            candidate_budget = request.estimate_budget(candidate_view)
        except (InvalidCheckpointBoundaryError, InvalidToolCallSequenceError):
            failed = replace(
                event,
                status="failed",
                failure_reason="invalid_tool_sequence",
                checkpoint_id=None,
                final_failure_reason="invalid_tool_sequence",
            )
            return _CandidateOutcome(
                candidate=candidate,
                event=failed,
                view=l3_request.view,
                input_tokens=request.estimate_budget(l3_request.view).input_tokens,
            )
        if candidate_budget.input_tokens >= target_tokens:
            failed = replace(
                event,
                status="failed",
                failure_reason="still_over_budget",
                checkpoint_id=None,
                final_failure_reason="still_over_budget",
            )
            return _CandidateOutcome(
                candidate=candidate,
                event=failed,
                view=l3_request.view,
                input_tokens=candidate_budget.input_tokens,
            )

        self.l3_service.commit_candidate(candidate, runtime_state=request.runtime_state)
        rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
        rebuilt_budget = request.estimate_budget(rebuilt_view)
        return _CandidateOutcome(
            candidate=candidate,
            event=event,
            view=rebuilt_view,
            input_tokens=rebuilt_budget.input_tokens,
        )

    def _hard_truncate(
        self,
        *,
        request: ContextCompactRequest,
        view: SessionView,
        target_tokens: int,
        before_tokens: int,
        before_failure_count: int,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
    ) -> _HardTruncateOutcome:
        """L3 失败时的兜底:按最近 N 轮截断早期对话并建摘要检查点。"""

        messages = [message for message in view.messages if message.role != "system_meta"]
        boundary = select_compaction_boundary(
            messages,
            current_turn=request.current_turn,
            recent_turn_window=self.config.recent_turn_window,
        )
        if boundary is None:
            return _HardTruncateOutcome(status="nothing_to_drop")
        covered_until_message_id, tail_start_message_id = boundary

        checkpoint = Checkpoint(
            id="",
            session_id=view.session_id,
            summary="[Earlier dialogue truncated — recent N turns kept]",
            tail_start_message_id=tail_start_message_id,
            covered_until_message_id=covered_until_message_id,
            source_fingerprint=source_fingerprint_for_view(view),
            sequence=max((existing.sequence for existing in view.checkpoints), default=0) + 1,
            metadata={
                "created_by": "hard_truncate",
                "recent_turn_window": self.config.recent_turn_window,
            },
        )
        candidate = LlmCompactCandidate(
            checkpoint=checkpoint,
            event=LlmCompactEvent(
                status="success",
                source_fingerprint=checkpoint.source_fingerprint,
                checkpoint_id=checkpoint.id,
            ),
        )
        truncated_view = _view_with_checkpoint(view, checkpoint)
        try:
            after_tokens = request.estimate_budget(truncated_view).input_tokens
        except (InvalidCheckpointBoundaryError, InvalidToolCallSequenceError):
            return _HardTruncateOutcome(status="nothing_to_drop")
        if after_tokens >= target_tokens:
            return _HardTruncateOutcome(status="over_budget")
        self.l3_service.commit_candidate(candidate, runtime_state=request.runtime_state)
        rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
        after_tokens = request.estimate_budget(rebuilt_view).input_tokens
        hard_truncate_event = replace(
            candidate.event,
            fallback_steps=[
                FallbackStep(
                    step=1,
                    reason="hard_truncate",
                    action="hard_truncate",
                    before_tokens=before_tokens,
                    after_tokens=after_tokens,
                    status="success",
                ).to_dict()
            ],
        )
        self._record_l3_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=hard_truncate_event,
        )
        self._record_auto_success_if_needed(request=request, mode=mode)
        return _HardTruncateOutcome(
            status="success",
            result=ContextCompactResult(
                status="success",
                reason="hard_truncate",
                view=rebuilt_view,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                programmatic_event=None,
                l3_event=hard_truncate_event,
            ),
        )

    def _run_fallback(
        self,
        *,
        request: ContextCompactRequest,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
        target_tokens: int,
        programmatic: CompactionResult,
        outcome: _CandidateOutcome,
        before_failure_count: int,
    ) -> ContextCompactResult:
        """按失败原因选择更强的规则压缩或更强摘要重试。"""
        reason = outcome.event.failure_reason or outcome.event.status
        action = self.fallback_policy.action_for(reason)
        steps: list[dict[str, object]] = []
        current_programmatic = programmatic
        before_tokens = request.budget.input_tokens

        if action == "stronger_programmatic":
            stronger = self.pipeline.compact(
                CompactionRequest(
                    view=programmatic.view,
                    target_tokens=target_tokens,
                    current_turn=request.current_turn,
                    estimate_tokens=lambda candidate: request.estimate_budget(candidate).input_tokens,
                    consumed_tool_result_part_ids=frozenset(request.runtime_state.consumed_tool_result_part_ids),
                    enabled_levels=("l1", "l2"),
                    l2_result_target_tokens=self.config.l2_result_target_tokens,
                    force_route_current_text=_force_route_current_text_for_trigger(trigger),
                )
            )
            self._record_programmatic_event(
                session_id=request.view.session_id,
                trigger=trigger,
                target_tokens=target_tokens,
                event=stronger.event,
            )
            stronger_tokens = request.estimate_budget(stronger.view).input_tokens
            stronger_status = "success" if stronger_tokens < target_tokens else "failed"
            steps.append(
                FallbackStep(
                    step=1,
                    reason=reason,
                    action=action,
                    before_tokens=outcome.input_tokens,
                    after_tokens=stronger_tokens,
                    status=stronger_status,
                    error=None if stronger_status == "success" else "still_over_budget",
                ).to_dict()
            )
            current_programmatic = stronger
            if stronger_status == "success":
                event = _with_fallback(
                    replace(outcome.event, status="success", failure_reason="fallback_success"),
                    fallback_steps=steps,
                    final_failure_reason=None,
                )
                self._record_l3_event(
                    session_id=request.view.session_id,
                    trigger=trigger,
                    target_tokens=target_tokens,
                    event=event,
                )
                self._record_auto_success_if_needed(request=request, mode=mode)
                return ContextCompactResult(
                    status="success",
                    reason=_result_reason(trigger=trigger, auto_reason=reason),
                    view=stronger.view,
                    before_tokens=before_tokens,
                    after_tokens=stronger_tokens,
                    programmatic_event=stronger.event,
                    l3_event=event,
                    fallback_steps=steps,
                )

        if action in {"stronger_programmatic", "retry_l3_stronger_summary"}:
            retry = self._generate_validate_commit(
                request=request,
                l3_request=LlmCompactRequest(
                    view=current_programmatic.view,
                    runtime_state=request.runtime_state,
                    consumed_tool_result_part_ids=frozenset(request.runtime_state.consumed_tool_result_part_ids),
                    mode=mode.value,
                    summary_mode="stronger",
                    current_turn=request.current_turn,
                    recent_turn_window=self.config.recent_turn_window,
                ),
                target_tokens=target_tokens,
            )
            steps.append(
                FallbackStep(
                    step=len(steps) + 1,
                    reason=retry.event.failure_reason or retry.event.status,
                    action="retry_l3_stronger_summary",
                    before_tokens=request.estimate_budget(current_programmatic.view).input_tokens,
                    after_tokens=retry.input_tokens,
                    status="success" if retry.event.status == "success" else "failed",
                    error=retry.event.failure_reason if retry.event.status != "success" else None,
                ).to_dict()
            )
            final_reason = None if retry.event.status == "success" else retry.event.failure_reason
            event = _with_fallback(retry.event, fallback_steps=steps, final_failure_reason=final_reason)
            if retry.event.status == "success":
                self._record_l3_event(
                    session_id=request.view.session_id,
                    trigger=trigger,
                    target_tokens=target_tokens,
                    event=event,
                )
                self._record_auto_success_if_needed(request=request, mode=mode)
                return ContextCompactResult(
                    status="success",
                    reason=_result_reason(trigger=trigger, auto_reason=reason),
                    view=retry.view,
                    before_tokens=before_tokens,
                    after_tokens=retry.input_tokens,
                    programmatic_event=current_programmatic.event,
                    l3_event=event,
                    fallback_steps=steps,
                    final_failure_reason=final_reason,
                )
            return self._final_l3_failure_or_hard_truncate(
                request=request,
                trigger=trigger,
                mode=mode,
                target_tokens=target_tokens,
                view=current_programmatic.view,
                before_tokens=before_tokens,
                before_failure_count=before_failure_count,
                programmatic=programmatic,
                after_tokens=retry.input_tokens,
                event=event,
                reason=final_reason or "failed",
                fallback_steps=steps,
            )

        steps.append(
            FallbackStep(
                step=1,
                reason=reason,
                action=action,
                before_tokens=outcome.input_tokens,
                after_tokens=outcome.input_tokens,
                status="failed",
                error=reason,
            ).to_dict()
        )
        return self._final_l3_failure_or_hard_truncate(
            request=request,
            trigger=trigger,
            mode=mode,
            target_tokens=target_tokens,
            view=current_programmatic.view,
            before_tokens=before_tokens,
            before_failure_count=before_failure_count,
            programmatic=programmatic,
            after_tokens=outcome.input_tokens,
            event=_with_fallback(outcome.event, fallback_steps=steps, final_failure_reason=reason),
            reason=reason,
            fallback_steps=steps,
        )

    def _final_l3_failure_or_hard_truncate(
        self,
        *,
        request: ContextCompactRequest,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
        target_tokens: int,
        view: SessionView,
        before_tokens: int,
        before_failure_count: int,
        programmatic: CompactionResult,
        after_tokens: int,
        event: LlmCompactEvent,
        reason: str,
        fallback_steps: list[dict[str, object]] | None = None,
    ) -> ContextCompactResult:
        """最终失败路径:先尝试硬截断,不行再记录失败。"""

        outcome = self._hard_truncate(
            request=request,
            view=view,
            target_tokens=target_tokens,
            before_tokens=before_tokens,
            before_failure_count=before_failure_count,
            trigger=trigger,
            mode=mode,
        )
        if outcome.status == "success":
            assert outcome.result is not None
            return outcome.result
        if outcome.status == "over_budget":
            reason = "still_over_budget_after_hard_truncate"
        return self._final_l3_failure(
            request=request,
            trigger=trigger,
            mode=mode,
            target_tokens=target_tokens,
            programmatic=programmatic,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            before_failure_count=before_failure_count,
            event=event,
            reason=reason,
            fallback_steps=fallback_steps,
        )

    def _final_l3_failure(
        self,
        *,
        request: ContextCompactRequest,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
        target_tokens: int,
        programmatic: CompactionResult,
        before_tokens: int,
        after_tokens: int,
        before_failure_count: int,
        event: LlmCompactEvent,
        reason: str,
        fallback_steps: list[dict[str, object]] | None = None,
    ) -> ContextCompactResult:
        """记录 L3 失败事件与自动失败计数,返回失败结果。"""
        event = replace(event, final_failure_reason=reason)
        self._record_l3_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=event,
        )
        self._record_auto_failure_if_needed(
            request=request,
            mode=mode,
            before_failure_count=before_failure_count,
            failure_reason=reason,
        )
        return ContextCompactResult(
            status="failed",
            reason=reason,
            view=programmatic.view,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            programmatic_event=programmatic.event,
            l3_event=event,
            fallback_steps=fallback_steps,
            final_failure_reason=reason,
        )

    def _unchanged(
        self,
        status: ManagerStatus,
        reason: str,
        request: ContextCompactRequest,
        tokens: int,
    ) -> ContextCompactResult:
        """构造未做变更的跳过/失败结果。"""
        return ContextCompactResult(status, reason, request.view, tokens, tokens)

    def _record_auto_failure_if_needed(
        self,
        *,
        request: ContextCompactRequest,
        mode: ContextCompactMode,
        before_failure_count: int,
        failure_reason: str,
    ) -> None:
        """自动模式下记录失败计数(仅当本次失败未使计数增长时)。"""
        if mode != ContextCompactMode.AUTO:
            return
        if request.runtime_state.auto_compact_failure_count > before_failure_count:
            return
        request.runtime_state.record_auto_compact_failure(failure_reason)

    def _record_auto_success_if_needed(
        self,
        *,
        request: ContextCompactRequest,
        mode: ContextCompactMode,
    ) -> None:
        """自动模式下记录一次成功压缩。"""
        if mode == ContextCompactMode.AUTO:
            request.runtime_state.record_auto_compact_success()

    def _record_programmatic_event(
        self,
        *,
        session_id: str,
        trigger: ContextWindowTrigger,
        target_tokens: int,
        event: CompactionEvent,
    ) -> None:
        """把规则式压缩事件写入会话。"""
        SessionEventWriter(store=self.store, session_id=session_id).append_compaction_completed(
            trigger=trigger.value,
            target_tokens=target_tokens,
            event=event,
        )

    def _record_l3_event(
        self,
        *,
        session_id: str,
        trigger: ContextWindowTrigger,
        target_tokens: int,
        event: LlmCompactEvent,
    ) -> None:
        """把 LLM 压缩事件写入会话。"""
        SessionEventWriter(store=self.store, session_id=session_id).append_llm_compaction_completed(
            trigger=trigger.value,
            target_tokens=target_tokens,
            event=event,
        )


def _target_tokens(request: ContextCompactRequest, trigger: ContextWindowTrigger) -> int:
    """确定压缩目标 token(优先显式目标,否则取低水位)。"""
    if request.target_tokens is not None:
        return request.target_tokens
    return request.budget.low_watermark


def _view_with_checkpoint(view: SessionView, checkpoint: Checkpoint) -> SessionView:
    """返回带新检查点的视图副本。"""
    return SessionView(
        session_id=view.session_id,
        messages=[AgentMessage.from_dict(message.to_dict()) for message in view.messages],
        checkpoints=[*view.checkpoints, Checkpoint.from_dict(checkpoint.to_dict())],
        metadata=dict(view.metadata),
        task_plan=view.task_plan,
    )


def _result_reason(*, trigger: ContextWindowTrigger, auto_reason: str) -> str:
    """压缩结果原因:AUTO 用触发决策原因,否则用触发名。"""
    return auto_reason if trigger == ContextWindowTrigger.AUTO else trigger.value


def _force_route_current_text_for_trigger(trigger: ContextWindowTrigger) -> bool:
    """手动/提示过长触发时是否强制把当前文本送入压缩路由。"""
    return trigger in {ContextWindowTrigger.MANUAL, ContextWindowTrigger.PROMPT_TOO_LONG}


def _with_fallback(
    event: LlmCompactEvent,
    *,
    fallback_steps: list[dict[str, object]],
    final_failure_reason: str | None,
) -> LlmCompactEvent:
    """给事件附加回退步骤与最终失败原因。"""
    return replace(
        event,
        fallback_steps=fallback_steps,
        final_failure_reason=final_failure_reason,
    )
