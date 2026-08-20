from __future__ import annotations

from lanscoder.app.subagent_panel_state import (
    FG_ID,
    SubagentRow,
    build_rows,
    can_enter_selection,
    has_running,
    move_selection,
    stop_target,
)


def _row(
    row_id: str,
    *,
    status: str = "running",
    cancellable: bool = True,
    cancel_requested: bool = False,
) -> SubagentRow:
    return SubagentRow(
        id=row_id,
        label=row_id,
        status=status,
        cancellable=cancellable,
        cancel_requested=cancel_requested,
    )


class _Job:
    def __init__(
        self,
        job_id: str,
        label: str | None,
        *,
        status: str = "running",
        cancel_requested: bool = False,
    ) -> None:
        self.id = job_id
        self.label = label
        self.tool_name = "delegate"
        self.status = status
        self.cancel_requested = cancel_requested


def test_build_rows_puts_foreground_first() -> None:
    fg = {
        "label": "researcher",
        "started_at": 0.0,
        "provider_calls": 0,
        "total_tokens": 0,
    }
    rows = build_rows(fg, [_Job("bg_0001", "reviewer")])
    assert [row.id for row in rows] == ["fg", "bg_0001"]
    assert rows[0].label == "researcher"
    assert rows[1].id == "bg_0001"
    assert rows[1].cancellable is True


def test_build_rows_empty_when_nothing_running() -> None:
    assert build_rows(None, []) == []


def test_build_rows_labels_cancelling_jobs() -> None:
    rows = build_rows(None, [_Job("bg_0001", "reviewer", cancel_requested=True)])
    assert rows[0].status == "cancelling"
    assert rows[0].cancel_requested is True


def test_move_selection_clamps_at_edges() -> None:
    rows = [_row(FG_ID), _row("bg_0001")]
    assert move_selection(rows, None, "down") == FG_ID
    assert move_selection(rows, FG_ID, "down") == "bg_0001"
    assert move_selection(rows, "bg_0001", "down") == "bg_0001"
    assert move_selection(rows, FG_ID, "up") == FG_ID
    assert move_selection(rows, None, "up") == "bg_0001"


def test_move_selection_relocates_when_selected_dropped() -> None:
    rows = [_row(FG_ID), _row("bg_0001")]
    assert move_selection(rows, "gone", "down") == FG_ID


def test_can_enter_selection_requires_running_and_nothing_to_recall() -> None:
    assert can_enter_selection([_row(FG_ID)], None) is True
    assert can_enter_selection([_row(FG_ID)], "") is True
    assert can_enter_selection([_row(FG_ID)], "older text") is False
    assert can_enter_selection([], None) is False
    assert can_enter_selection([_row(FG_ID, cancellable=False)], None) is False


def test_has_running() -> None:
    assert has_running([_row(FG_ID)]) is True
    assert has_running([_row(FG_ID, cancellable=False)]) is False
    assert has_running([]) is False


def test_stop_target_returns_selected_cancellable_row() -> None:
    rows = [_row(FG_ID), _row("bg_0001", cancellable=False)]
    assert stop_target(rows, FG_ID) == FG_ID
    assert stop_target(rows, "bg_0001") is None
    assert stop_target(rows, None) is None
    assert stop_target(rows, "missing") is None
