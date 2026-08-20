from __future__ import annotations

from typing import Any

from lanscoder.providers.types import ToolDefinition
from lanscoder.subagent.types import SubagentRequest, SubagentRunner
from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from lanscoder.utils.schema import object_schema


def create_delegate_tool(
    runner: SubagentRunner,
    *,
    parent_session_id: str,
) -> Tool:

    def delegate(
        role: str,
        task: str,
        parent_summary: str | None = None,
        path_hints: list[str] | None = None,
        isolate_worktree: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        if kwargs:
            return make_error_result("delegate", f"未知参数：{', '.join(sorted(kwargs))}")
        normalized_role = str(role).strip()
        if runner.profile(normalized_role) is None:
            return make_error_result("delegate", f"未知子代理角色：{normalized_role}", role=normalized_role)
        normalized_task = str(task or "").strip()
        if not normalized_task:
            return make_error_result("delegate", "task 不能为空")
        hints = [str(item).strip() for item in path_hints or [] if str(item).strip()]
        request = SubagentRequest(
            role=normalized_role,  # type: ignore[arg-type]
            task=normalized_task,
            parent_session_id=parent_session_id,
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
            _format_delegate_result(
                result.summary,
                result.child_session_id,
                total_tokens=result.total_tokens,
                provider_calls=result.provider_calls,
                elapsed_seconds=result.elapsed_seconds,
            ),
            **result.to_data(),
        )

    return Tool(
        definition=ToolDefinition(
            name="delegate",
            description=(
                "Run a restricted child LansCoder subagent with a fresh context. Use for independent "
                "research, review, validation, or isolated implementation work. Do not use for nested "
                "delegation. researcher/reviewer/tester can run in background directly; coder can run "
                "in background only when git worktree isolation is available. "
                "When you have multiple independent sub-tasks, dispatch them all at once in a single "
                "turn by emitting multiple delegate tool calls (each with run_in_background=true), "
                "so they run in parallel instead of waiting for each one to finish."
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


def _format_delegate_result(
    summary: str,
    child_session_id: str,
    *,
    total_tokens: int | None = None,
    provider_calls: int | None = None,
    elapsed_seconds: float | None = None,
) -> str:
    meta: list[str] = []
    if elapsed_seconds is not None:
        meta.append(f"{elapsed_seconds:.1f}s")
    if provider_calls is not None:
        meta.append(f"{provider_calls} calls")
    if total_tokens is not None and total_tokens > 0:
        meta.append(f"{total_tokens / 1000:.1f}k tokens" if total_tokens >= 1000 else f"{total_tokens} tokens")
    header = f"Subagent {child_session_id} completed"
    if meta:
        header += " · " + " · ".join(meta)
    return f"{header}\n\n{summary}"
