from __future__ import annotations

import sys
from pathlib import Path

from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from lanscoder.utils.introspection import tool_from_function
from lanscoder.utils.execution_sandbox import ExecutionSandbox
from lanscoder.utils.sandbox_access import SandboxAccess


def create_python_exec_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:

    sandbox = ExecutionSandbox(root, access=access)

    def python_exec(code: str, cwd: str = ".", timeout_seconds: int = 30, max_output_chars: int = 20000) -> ToolResult:
        """在项目内执行 Python 代码；高风险，需显式启用。"""

        if timeout_seconds <= 0:
            return make_error_result("python_exec", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("python_exec", "max_output_chars 必须大于 0")

        try:
            workdir = sandbox.resolve_cwd(cwd)
        except ValueError as exc:
            return make_error_result("python_exec", str(exc))

        result = sandbox.run(
            [sys.executable, "-c", code],
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )

        data = {
            "cwd": sandbox.relative(workdir) or ".",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        }

        if result.error:
            return make_error_result("python_exec", result.error, **data)
        if not result.ok:
            return make_error_result("python_exec", f"Python 退出码为 {result.exit_code}", **data)

        content = result.stdout.strip() or result.stderr.strip() or f"Python 退出码：{result.exit_code}"
        return make_text_result("python_exec", content, **data)

    return tool_from_function(python_exec)
