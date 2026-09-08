from __future__ import annotations

from dataclasses import dataclass
from lanscoder.session.branch import SessionBranchContext

from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.planning.models import TaskPlan
from lanscoder.planning.projection import project_plan
from lanscoder.planning.reducer import (
    ReductionResult,
    TaskPlanCommandError,
    create_tasks,
    revise_tasks,
    update_tasks,
)


@dataclass(frozen=True, slots=True)
class TaskPlanMutation:
    plan: TaskPlan
    projection: dict[str, object]
    changed: bool
    changes: tuple[dict[str, object], ...] = ()


class TaskPlanService:
    def __init__(
        self,
        *,
        store: JsonlSessionStore,
        writer: SessionEventWriter,
    ) -> None:
        self._store = store
        self._writer = writer

    def current(self, *, branch_context: SessionBranchContext | None = None) -> TaskPlan | None:
        return self._store.rebuild_session_view(
            self._writer.session_id,
            branch_context=branch_context,
        ).task_plan

    def create(
        self,
        *,
        mode: str,
        expected_revision: int,
        tasks: object,
        start_new_plan: bool = False,
        branch_context: SessionBranchContext | None = None,
    ) -> TaskPlanMutation:
        result = self._writer.mutate_task_plan(
            expected_revision=expected_revision,
            operation="create",
            branch_context=branch_context,
            reducer=lambda current_plan: create_tasks(
                current_plan=current_plan,
                expected_revision=expected_revision,
                mode=mode,
                tasks=tasks,
                start_new_plan=start_new_plan,
            ),
        )
        return self._mutation(result)

    def update(
        self,
        *,
        expected_revision: int,
        updates: object,
        branch_context: SessionBranchContext | None = None,
    ) -> TaskPlanMutation:
        result = self._writer.mutate_task_plan(
            expected_revision=expected_revision,
            operation="update",
            branch_context=branch_context,
            reducer=lambda current_plan: update_tasks(
                plan=self._require_plan(current_plan, "update"),
                expected_revision=expected_revision,
                updates=updates,
            ),
        )
        return self._mutation(result)

    def revise(
        self,
        *,
        expected_revision: int,
        revisions: object,
        branch_context: SessionBranchContext | None = None,
    ) -> TaskPlanMutation:
        result = self._writer.mutate_task_plan(
            expected_revision=expected_revision,
            operation="revise",
            branch_context=branch_context,
            reducer=lambda current_plan: revise_tasks(
                plan=self._require_plan(current_plan, "revise"),
                expected_revision=expected_revision,
                revisions=revisions,
            ),
        )
        return self._mutation(result)

    @staticmethod
    def _require_plan(plan: TaskPlan | None, operation: str) -> TaskPlan:
        if plan is None:
            raise TaskPlanCommandError(f"cannot {operation}: no current task plan; create one first")
        return plan

    @staticmethod
    def _mutation(result: ReductionResult) -> TaskPlanMutation:
        return TaskPlanMutation(
            plan=result.plan,
            projection=project_plan(result.plan),
            changed=result.changed,
            changes=result.changes,
        )
