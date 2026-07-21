"""Incremental task-plan creation tool."""

from __future__ import annotations

from collections.abc import Callable

from firstcoder.planning.reducer import TaskPlanCommandError, TaskPlanRevisionConflict
from firstcoder.planning.service import TaskPlanService
from firstcoder.planning.service import TaskPlanMutation
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema


def create_task_create_tool(service: TaskPlanService) -> Tool:
    def task_create(*, mode: str, expected_revision: int, tasks: object):
        return execute_task_plan_mutation(
            "task_create",
            lambda: service.create(
                mode=mode,
                expected_revision=expected_revision,
                tasks=tasks,
            ),
        )

    parameters = object_schema(
        {
            "mode": {"type": "string", "enum": ["linear", "dag"]},
            "expected_revision": {"type": "integer", "minimum": 0},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                        },
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "owner": {"type": ["string", "null"]},
                    },
                    "required": ["id", "content"],
                    "additionalProperties": False,
                },
            },
        },
        required=["mode", "expected_revision", "tasks"],
    )
    parameters["additionalProperties"] = False
    return Tool(
        definition=ToolDefinition(
            name="task_create",
            description="Create or append tasks by stable task ID without replacing existing work.",
            parameters=parameters,
        ),
        executor=task_create,
    )


def execute_task_plan_mutation(
    tool_name: str,
    mutate: Callable[[], TaskPlanMutation],
) -> ToolResult:
    """Format a service mutation without duplicating plan logic in tools."""

    try:
        mutation = mutate()
    except TaskPlanRevisionConflict as error:
        return make_error_result(
            tool_name,
            f"Revision conflict: expected {error.expected}, actual {error.actual}. "
            "Call task_list, then retry with the latest revision.",
            expected_revision=error.expected,
            actual_revision=error.actual,
        )
    except (TaskPlanCommandError, ValueError, TypeError) as error:
        return make_error_result(tool_name, str(error))

    return make_text_result(
        tool_name,
        f"Task plan revision {mutation.plan.revision}",
        revision=mutation.plan.revision,
        changed=mutation.changed,
        changes=[dict(change) for change in mutation.changes],
        snapshot=mutation.plan.to_dict(),
        projection=mutation.projection,
    )
