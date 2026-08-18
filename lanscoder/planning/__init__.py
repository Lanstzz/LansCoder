"""Unified task-plan domain types and pure derived views.

The package deliberately contains no agent/runtime imports: TaskPlan structure,
validation, and projections remain deterministic and independently testable.
"""

from __future__ import annotations

from lanscoder.planning.models import (
    Task,
    TaskPlan,
    TaskPlanError,
    TaskPlanMode,
    TaskStatus,
)
from lanscoder.planning.projection import (
    blocked_task_ids,
    effective_dependencies,
    ordered_tasks,
    project_plan,
    ready_task_ids,
    topological_levels,
)
from lanscoder.planning.validation import validate_plan

__all__ = [
    "Task",
    "TaskPlan",
    "TaskPlanError",
    "TaskPlanMode",
    "TaskStatus",
    "blocked_task_ids",
    "effective_dependencies",
    "ordered_tasks",
    "project_plan",
    "ready_task_ids",
    "topological_levels",
    "validate_plan",
]
