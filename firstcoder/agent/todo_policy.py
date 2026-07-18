"""Todo reminder and self-check policy for the agent loop.

Keeps progress/planning nudges out of AgentLoop so the main orchestrator only
decides when to inject a reminder string.
"""

from __future__ import annotations

from firstcoder.agent.session import AgentSession

STALE_TOOL_RESULT_THRESHOLD = 3
MISSING_TOOL_RESULT_THRESHOLD = 2


class TodoPolicy:
    """Derive runtime todo reminders from the session transcript."""

    def __init__(self, session: AgentSession) -> None:
        self.session = session
        self._last_stale_reminder_count = 0
        self._missing_plan_reminded = False

    def next_reminder(self) -> str | None:
        return self.planning_reminder() or self.progress_reminder()

    def self_check_prompt(self) -> str | None:
        unfinished = self.latest_unfinished_todos()
        if not unfinished:
            return None
        lines = [
            "Self-check before final answer: there are unfinished todo items.",
            "Continue the task or explicitly explain why these items no longer need action. Do not claim completion while they remain unresolved.",
        ]
        for item in unfinished:
            lines.append(f"- [{item.get('status', 'pending')}] {item.get('content', '')}")
        return "\n".join(lines)

    def progress_reminder(self) -> str | None:
        unfinished = self.latest_unfinished_todos()
        if not unfinished:
            return None
        stale_count = self.non_todo_tool_results_since_latest_todo()
        if stale_count < STALE_TOOL_RESULT_THRESHOLD:
            return None
        if stale_count - self._last_stale_reminder_count < STALE_TOOL_RESULT_THRESHOLD:
            return None
        self._last_stale_reminder_count = stale_count
        lines = [
            "Todo progress reminder: several tools have run since the todo list was last updated.",
            "If progress changed, call todo with the complete current list and update only the relevant statuses.",
            "Do not rewrite, rephrase, split, merge, or reorder the plan unless the plan itself is wrong. Continue if the current in_progress item is still accurate.",
        ]
        for item in unfinished:
            lines.append(f"- [{item.get('status', 'pending')}] {item.get('content', '')}")
        return "\n".join(lines)

    def planning_reminder(self) -> str | None:
        if self._missing_plan_reminded:
            return None
        if "todo" not in self.session.tool_registry.names():
            return None
        if self.has_todo_result():
            return None
        non_todo_count = self.non_todo_tool_results_since_latest_todo()
        if non_todo_count < MISSING_TOOL_RESULT_THRESHOLD:
            return None
        self._missing_plan_reminded = True
        return "\n".join(
            [
                "Todo planning reminder: this has become multi-step work, but no todo plan exists yet.",
                "Call todo with a complete 3-7 item plan before continuing implementation. Use concrete, verifiable items and keep exactly one in_progress. Submit the complete current list on every later update.",
            ]
        )

    def latest_unfinished_todos(self) -> list[dict[str, object]]:
        view = self.session.rebuild_view()
        if not self._view_has_active_task_todos(view):
            return []
        return [
            item
            for item in view.todos
            if isinstance(item, dict) and item.get("status") in {"pending", "in_progress"}
        ]

    def has_todo_result(self) -> bool:
        view = self.session.rebuild_view()
        return self._view_has_active_task_todos(view)

    def _view_has_active_task_todos(self, view) -> bool:
        if not view.todo_initialized:
            return False
        active_task_hash = self.session.runtime_state.active_task_hash
        if view.todo_task_hash is not None and active_task_hash is not None:
            return view.todo_task_hash == active_task_hash
        return True

    def non_todo_tool_results_since_latest_todo(self) -> int:
        view = self.session.rebuild_view()
        count = 0
        for message in reversed(view.messages):
            for part in reversed(message.parts):
                if part.kind != "tool_result":
                    continue
                if part.metadata.get("tool_name") == "todo":
                    return count
                count += 1
        return count
