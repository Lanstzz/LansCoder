from __future__ import annotations

from pathlib import Path

from lanscoder.permissions.types import PermissionAction
from lanscoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from lanscoder.utils.introspection import tool_from_function
from lanscoder.utils.execution_sandbox import ExecutionSandbox
from lanscoder.utils.sandbox_access import SandboxAccess

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_CHARS = 20000


def create_shell_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:

    sandbox = ExecutionSandbox(root, access=access)

    def shell(
        command: str,
        cwd: str = ".",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> ToolResult:
        """在项目内执行 shell 命令；高风险，需显式启用。"""

        if timeout_seconds <= 0:
            return make_error_result("shell", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("shell", "max_output_chars 必须大于 0")

        try:
            workdir = sandbox.resolve_cwd(cwd)
        except ValueError as exc:
            return make_error_result("shell", str(exc))

        result = sandbox.run(
            command,
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            shell=True,
        )

        data = {
            "command": command,
            "cwd": sandbox.relative(workdir) or ".",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        }

        if result.error:
            content = result.error
            output_sections = []
            if result.stdout:
                output_sections.append(f"stdout:\n{result.stdout.rstrip()}")
            if result.stderr:
                output_sections.append(f"stderr:\n{result.stderr.rstrip()}")
            if output_sections:
                content = f"{content}\n\n" + "\n\n".join(output_sections)
            return ToolResult(name="shell", ok=False, content=content, data=data, error=result.error)
        if not result.ok:
            return make_error_result("shell", f"命令退出码为 {result.exit_code}", **data)

        content = result.stdout.strip() or result.stderr.strip() or f"命令退出码：{result.exit_code}"
        return make_text_result("shell", content, **data)

    tool = tool_from_function(shell)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_arg="command",
        cwd_arg="cwd",
        reason="执行 shell 命令需要用户确认。",
    )
    return tool
