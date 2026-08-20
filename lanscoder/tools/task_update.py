from __future__ import annotations

from lanscoder.planning.service import TaskPlanService
from lanscoder.providers.types import ToolDefinition
from lanscoder.tools.task_plan_support import execute_task_plan_mutation
from lanscoder.tools.types import Tool
from lanscoder.utils.schema import object_schema


def create_task_update_tool(service: TaskPlanService) -> Tool:
    def task_update(*, expected_revision: int, updates: object):
        return execute_task_plan_mutation(
            "task_update",
            lambda: service.update(expected_revision=expected_revision, updates=updates),
        )

    parameters = object_schema(
        {
            "expected_revision": {"type": "integer", "minimum": 0},
            "updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                        },
                        "owner": {"type": ["string", "null"]},
                        "add_depends_on": {"type": "array", "items": {"type": "string"}},
                        "remove_depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        required=["expected_revision", "updates"],
    )
    parameters["additionalProperties"] = False
    return Tool(
        definition=ToolDefinition(
            name="task_update",
            description="Atomically update status, owner, or dependencies by stable task ID.",
            parameters=parameters,
        ),
        executor=task_update,
    )
