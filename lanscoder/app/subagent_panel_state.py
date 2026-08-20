"""Subagent panel selection state machine (pure, TUI-agnostic).

Selection logic lives here so it is unit-testable without a running Textual
app.  Stable ids: the foreground subagent is ``FG_ID``; each background job
uses its own job id.  Stopping a foreground subagent ends the parent turn
(the two share a thread and cancellation token) — that is an architectural
constraint, not a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FG_ID = "fg"


@dataclass(frozen=True)
class SubagentRow:
    id: str
    label: str
    status: str
    cancellable: bool
    cancel_requested: bool


def build_rows(foreground: dict[str, Any] | None, jobs: list[Any]) -> list[SubagentRow]:
    rows: list[SubagentRow] = []
    if foreground is not None:
        rows.append(
            SubagentRow(
                id=FG_ID,
                label=foreground.get("label") or "delegate",
                status="running",
                cancellable=True,
                cancel_requested=False,
            )
        )
    for job in jobs:
        rows.append(
            SubagentRow(
                id=job.id,
                label=job.label or job.tool_name,
                status="cancelling" if job.cancel_requested else job.status,
                cancellable=job.status == "running",
                cancel_requested=job.cancel_requested,
            )
        )
    return rows


def has_running(rows: list[SubagentRow]) -> bool:
    return any(row.cancellable for row in rows)


def move_selection(
    rows: list[SubagentRow], selected: str | None, direction: str
) -> str | None:
    if not rows:
        return None
    if selected is None:
        return rows[0].id if direction == "down" else rows[-1].id
    index_by_id = {row.id: i for i, row in enumerate(rows)}
    index = index_by_id.get(selected)
    if index is None:
        return rows[0].id
    if direction == "down":
        index = min(len(rows) - 1, index + 1)
    elif direction == "up":
        index = max(0, index - 1)
    return rows[index].id


def can_enter_selection(rows: list[SubagentRow], down_recall: str | None) -> bool:
    """Down arrow may enter selection mode when the input history has nothing
    newer to recall (``down_recall`` is None or empty) and a subagent is running."""
    return not down_recall and has_running(rows)


def stop_target(rows: list[SubagentRow], selected: str | None) -> str | None:
    for row in rows:
        if row.id == selected and row.cancellable:
            return row.id
    return None
