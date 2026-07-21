"""Delegate tool for running restricted FirstCoder subagents."""

from __future__ import annotations

from typing import Any

from firstcoder.agent.subagent import (
    SUBAGENT_PROFILES,
    SubagentRequest,
    SubagentRunner,
    SubagentRole,
)
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema


def create_delegate_tool(
    runner: SubagentRunner,
    *,
    parent_session_id: str,
    parent_task_hash: str | None = None,
) -> Tool:
    """Create the parent-facing delegate tool.

    Background execution itself is handled by ToolExecutor's generic Phase 1
    `run_in_background` control field.  The delegate executor keeps foreground
    semantics and receives cleaned arguments.
    """

    def delegate(
        role: str,
        task: str,
        parent_summary: str | None = None,
        path_hints: list[str] | None = None,
        isolate_worktree: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        # ``isolate_worktree`` is an internal control field injected by the parent
        # ToolExecutor when it backgrounds a mutation-capable role; it is not part of
        # the model-visible schema.
        if kwargs:
            return make_error_result("delegate", f"未知参数：{', '.join(sorted(kwargs))}")
        normalized_role = str(role).strip()
        if normalized_role not in runner.profile_map:
            return make_error_result("delegate", f"未知子代理角色：{normalized_role}", role=normalized_role)
        normalized_task = str(task or "").strip()
        if not normalized_task:
            return make_error_result("delegate", "task 不能为空")
        hints = [str(item).strip() for item in path_hints or [] if str(item).strip()]
        request = SubagentRequest(
            role=normalized_role,  # type: ignore[arg-type]
            task=normalized_task,
            parent_session_id=parent_session_id,
            parent_task_hash=parent_task_hash,
            parent_summary=parent_summary,
            path_hints=hints,
            run_in_background=False,
            isolate_worktree=bool(isolate_worktree),
        )
        result = runner.run(request)
        if not result.ok:
            return make_error_result("delegate", result.summary, **result.to_data())
        return make_text_result(
            "delegate",
            _format_delegate_result(result.summary, result.child_session_id),
            **result.to_data(),
        )

    return Tool(
        definition=ToolDefinition(
            name="delegate",
            description=(
                "Run a restricted child FirstCoder subagent with a fresh context. Use for independent "
                "research, review, validation, or isolated implementation work. Do not use for nested "
                "delegation. researcher/reviewer/tester can run in background directly; coder can run "
                "in background only when git worktree isolation is available."
            ),
            parameters=object_schema(
                {
                    "role": {
                        "type": "string",
                        "enum": ["researcher", "reviewer", "tester", "coder"],
                        "description": "Subagent profile to run.",
                    },
                    "task": {"type": "string", "description": "Concrete task for the child agent."},
                    "parent_summary": {
                        "type": "string",
                        "description": "Optional compact context from the parent.",
                    },
                    "path_hints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional workspace paths to inspect.",
                    },
                },
                required=["role", "task"],
            ),
        ),
        executor=delegate,
    )


def role_allows_background(role: str) -> bool:
    profile = SUBAGENT_PROFILES.get(str(role).strip())
    return bool(profile and profile.allow_background)


def role_requires_worktree(role: str) -> bool:
    profile = SUBAGENT_PROFILES.get(str(role).strip())
    return bool(profile and profile.requires_worktree)


def _format_delegate_result(summary: str, child_session_id: str) -> str:
    return f"Subagent {child_session_id} completed.\n\n{summary}"
