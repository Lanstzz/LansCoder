"""上下文命令处理器:实现 /context、/compact、/compact status 三个斜杠命令。"""

from __future__ import annotations

from lanscoder.utils.text import display_value

from dataclasses import dataclass
from typing import Any, Protocol

from lanscoder.context.inspector import ContextInspectionReport, ContextInspector
from lanscoder.app.ports import ContextManagerLike
from lanscoder.context.manager import ContextCompactRequest, ContextCompactResult, ContextWindowTrigger
from lanscoder.context.models import SessionView
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.token_budget import ContextBudget


class SessionLike(Protocol):
    """会话的最小接口:可重建视图并暴露运行时状态。"""

    session_id: str
    runtime_state: SessionRuntimeState
    current_turn: int

    def rebuild_view(self) -> SessionView: ...


class BudgetProvider(Protocol):
    """按会话视图提供上下文预算的调用签名。"""

    def __call__(self, view: SessionView) -> ContextBudget: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """斜杠命令处理结果:是否已处理、输出文本与后续 UI 动作。"""

    handled: bool
    output: str = ""
    action: dict[str, Any] | None = None


@dataclass(slots=True)
class ContextCommandHandler:
    """处理上下文相关斜杠命令:检查上下文状态、手动触发压缩。"""

    session: SessionLike
    budget_provider: BudgetProvider
    context_manager: ContextManagerLike | None = None
    inspector: ContextInspector = ContextInspector()

    def commands(self) -> list[tuple[str, str]]:
        """声明支持的斜杠命令及其帮助文案。"""
        return [
            ("/context", "Inspect context state."),
            ("/compact status", "Show compaction status."),
            ("/compact", "Compact context now."),
        ]

    def handle(self, text: str) -> CommandResult:
        """按规范化命令分派到检查/压缩分支,未知命令返回未处理。"""
        command = text.strip()
        if not command.startswith("/"):
            return CommandResult(handled=False)

        normalized = " ".join(command.split())
        if normalized == "/context":
            report = self._inspect()
            return CommandResult(handled=True, output=_render_context_report(report))

        if normalized == "/compact status":
            report = self._inspect()
            return CommandResult(handled=True, output=_render_compact_status(report))

        if normalized == "/compact":
            return CommandResult(handled=True, output=self._manual_compact())

        return CommandResult(handled=False)

    def _inspect(self) -> ContextInspectionReport:
        """对当前会话做上下文检查并返回报告。"""
        view = self.session.rebuild_view()
        return self.inspector.inspect(
            view,
            self.session.runtime_state,
            budget=self.budget_provider(view),
        )

    def _manual_compact(self) -> str:
        """手动触发上下文压缩,返回结果文本。"""
        if self.context_manager is None:
            return "Manual compact unavailable: context manager is not configured"

        view = self.session.rebuild_view()
        budget = self.budget_provider(view)
        result = self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=view,
                runtime_state=self.session.runtime_state,
                budget=budget,
                estimate_budget=self.budget_provider,
                trigger=ContextWindowTrigger.MANUAL,
                mode="manual",
                current_turn=self.session.current_turn,
                target_tokens=_manual_target_tokens(budget),
            )
        )
        if _is_noop_compact(result):
            return f"Manual compact skipped: {result.programmatic_event.reason} " f"({result.before_tokens} -> {result.after_tokens} tokens)"
        return f"Manual compact {result.status}: {result.reason} " f"({result.before_tokens} -> {result.after_tokens} tokens)"


def _render_context_report(report: ContextInspectionReport) -> str:
    """把上下文检查报告渲染为多行文本。"""
    lines = [
        f"Session: {report.session_id}",
        f"Model window: {report.context_window} ({report.context_window_source})",
        f"Output reserve: {report.output_reserve}",
        f"Fixed tokens: {report.fixed_tokens}",
        f"History tokens: {report.history_tokens}",
        f"Input tokens: {report.input_tokens}",
        f"High watermark: {report.high_watermark}",
        f"Low watermark: {report.low_watermark}",
        f"Unconsumed tool results: {report.unconsumed_tool_result_count}",
        f"Tail messages: {report.tail_message_count}",
        f"Latest checkpoint: {display_value(report.latest_checkpoint_id)}",
        f"Checkpoint boundary: {report.checkpoint_boundary_status}",
        f"Archives: {report.archive_count}",
        f"System prompt fingerprint: {display_value(report.system_prompt_fingerprint)}",
    ]
    return "\n".join(lines)


def _render_compact_status(report: ContextInspectionReport) -> str:
    """把压缩状态报告渲染为多行文本。"""
    lines = [
        f"Auto compact: {report.auto_compact_status}",
        f"Disabled until: {display_value(report.auto_compact_disabled_until)}",
        f"Last failure: {display_value(report.last_failure_reason)}",
        f"Last input fingerprint: {display_value(report.last_compaction_input_fingerprint)}",
        f"Input tokens: {report.input_tokens}",
        f"Tail messages: {report.tail_message_count}",
        f"Latest checkpoint: {display_value(report.latest_checkpoint_id)}",
        "Recent compactions:",
    ]
    if not report.recent_compaction_events:
        lines.append("- none")
    else:
        for event in report.recent_compaction_events:
            lines.append("- " f"{event.get('event_type')} " f"{event.get('trigger')} " f"{event.get('status')} " f"{display_value(event.get('reason'))}")
    return "\n".join(lines)


def _manual_target_tokens(budget: ContextBudget) -> int | None:
    """按预算计算手动压缩目标 token,输入过小时返回 None 表示不压缩。"""
    if budget.input_tokens <= 2_000:
        return None
    proposed = min(budget.low_watermark - 1, int(budget.input_tokens * 0.6))
    target = max(budget.fixed_tokens + 1, proposed)
    return target if target < budget.input_tokens else None


def _is_noop_compact(result: ContextCompactResult) -> bool:
    """判断一次压缩是否无操作:事件标记 noop 且前后 token 数不变。"""
    return result.programmatic_event is not None and result.programmatic_event.noop and result.l3_event is None and result.before_tokens == result.after_tokens
